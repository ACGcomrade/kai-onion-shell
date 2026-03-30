"""
file_reader.py — Read-only file access for Onion Shell context.

At query time, finds and reads files that are relevant to the user's question:
  1. Files explicitly mentioned (by path or bare name) in the question
  2. The file currently open in the active code editor (from window title)

All access is read-only. No file is written or modified.
"""
from __future__ import annotations
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional

# Extensions considered "text" and safe to read
_TEXT_EXTS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs',
    '.md', '.txt', '.rst', '.log',
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.env',
    '.sh', '.bash', '.zsh', '.fish',
    '.go', '.rs', '.java', '.cpp', '.c', '.h', '.hpp',
    '.css', '.scss', '.sass', '.html', '.htm', '.xml', '.svg',
    '.sql', '.graphql', '.gql',
    '.rb', '.php', '.swift', '.kt', '.dart', '.lua',
    '.r', '.jl', '.m',       # R, Julia, MATLAB
    '.tf', '.hcl',           # Terraform
    '.proto',
    '',                      # no extension: Makefile, Dockerfile, etc.
}

_SKIP_DIRS = {
    'node_modules', '__pycache__', '.git', '.svn', 'vendor',
    'Cache', 'Caches', '.Trash', 'DerivedData', 'build', 'dist',
}

MAX_FILE_BYTES = 500_000   # skip files larger than 500 KB
MAX_CHARS      = 8_000     # truncate content to this in context
MAX_FILES      = 3         # max files to include per query


def _is_readable(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        if path.stat().st_size > MAX_FILE_BYTES:
            return False
        ext = path.suffix.lower()
        if ext not in _TEXT_EXTS and path.name not in {
            'Makefile', 'Dockerfile', 'Gemfile', 'Rakefile',
            'Procfile', 'Brewfile', 'Pipfile', 'Justfile',
        }:
            return False
        # Skip if inside a bad directory
        for part in path.parts:
            if part in _SKIP_DIRS:
                return False
        return True
    except OSError:
        return False


def read_file(path: Path, max_chars: int = MAX_CHARS) -> Optional[str]:
    """Read file, return text (truncated if large) or None on error."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
        if len(text) <= max_chars:
            return text
        # Truncate at clean line boundary
        truncated = text[:max_chars]
        last_nl = truncated.rfind('\n')
        if last_nl > max_chars // 2:
            truncated = truncated[:last_nl]
        total_lines = text.count('\n') + 1
        shown_lines = truncated.count('\n') + 1
        return truncated + f"\n... [truncated: showing {shown_lines}/{total_lines} lines]"
    except OSError:
        return None


def _find_by_spotlight(name: str) -> list[Path]:
    """Use macOS Spotlight to locate a file by name (searches all indexed volumes)."""
    if platform.system() != "Darwin":
        return []
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemFSName == '{name}'"],
            capture_output=True, text=True, timeout=4,
        )
        candidates = []
        for line in result.stdout.splitlines():
            p = Path(line.strip())
            if _is_readable(p):
                candidates.append(p)
        # Sort by most recently modified
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[:5]
    except Exception:
        return []


def _find_by_walk(name: str) -> list[Path]:
    """Fallback: walk common directories to find a file by name."""
    search_roots = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "Downloads",
        Path.home(),
    ]
    found = []
    for root in search_roots:
        if not root.exists():
            continue
        for p in root.rglob(name):
            if _is_readable(p):
                found.append(p)
            if len(found) >= 5:
                break
    return found


def find_file(name_or_path: str) -> Optional[Path]:
    """
    Given a filename or path string, return the best-matching readable Path.
    Tries: absolute path → ~/... → Spotlight → filesystem walk.
    """
    if not name_or_path:
        return None

    # Already an absolute path
    if name_or_path.startswith('/'):
        p = Path(name_or_path)
        return p if _is_readable(p) else None

    # ~/... path
    if name_or_path.startswith('~/'):
        p = Path(name_or_path).expanduser()
        return p if _is_readable(p) else None

    # Bare filename — search by Spotlight then walk
    candidates = _find_by_spotlight(name_or_path) or _find_by_walk(name_or_path)
    return candidates[0] if candidates else None


def extract_paths_from_question(question: str) -> list[str]:
    """
    Extract file references from a user question.
    Returns list of name/path strings (not yet resolved to Path).
    """
    found = []
    seen = set()

    def add(s: str):
        s = s.strip().rstrip('.,;:!?"\')]}')
        if s and s not in seen:
            seen.add(s)
            found.append(s)

    # Absolute paths: /foo/bar/baz.py
    for m in re.finditer(r'/[\w.\-/]+\.\w{1,8}', question):
        add(m.group())

    # ~/... paths
    for m in re.finditer(r'~/[\w.\-/]+\.\w{1,8}', question):
        add(m.group())

    # Bare filenames with extension: store.py, config.toml, README.md
    # Use a pattern that works adjacent to Chinese/unicode chars (no \b reliance)
    # Match filenames: allow Chinese/unicode immediately after (e.g. "store.py里")
    # but reject if followed by ASCII word chars (e.g. "file.python3")
    for m in re.finditer(r'(?<![/.\-\w])([\w][\w\-]*\.[a-zA-Z]{2,6})(?![a-zA-Z0-9/.\-])', question):
        name = m.group(1)
        # Skip: version numbers, domains, or non-text extensions
        if re.match(r'^\d', name):
            continue
        ext = '.' + name.rsplit('.', 1)[-1].lower()
        if ext in _TEXT_EXTS:
            add(name)

    return found[:MAX_FILES * 2]


def extract_editor_filename(window_title: str, app_name: str) -> Optional[str]:
    """
    Try to extract the open filename from an editor's window title.
    VSCode: "store.py — kai onion shell"  or  "● store.py — folder"
    Xcode:  "MyProject — MyFile.swift"
    Sublime: "store.py"
    """
    if not window_title:
        return None
    # Strip leading ●/•/✎ (unsaved indicator)
    title = re.sub(r'^[●•✎\*]\s*', '', window_title.strip())
    # Try: "filename — folder" or "filename - folder"
    for sep in (' — ', ' – ', ' - '):
        if sep in title:
            candidate = title.split(sep)[0].strip()
            if '.' in candidate and len(candidate) < 60:
                return candidate
    # No separator — the whole title might be the filename
    if '.' in title and len(title) < 60 and '\n' not in title:
        return title
    return None


def gather_file_context(question: str, window_title: str = "",
                        app_name: str = "",
                        max_files: int = MAX_FILES) -> list[dict]:
    """
    Main entry point. Returns list of dicts:
      {"path": Path, "content": str, "source": "question"|"editor"}
    Up to max_files files, read-only.
    """
    results = []
    seen_paths: set[Path] = set()

    def try_add(name_or_path: str, source: str):
        if len(results) >= max_files:
            return
        p = find_file(name_or_path)
        if p and p not in seen_paths:
            content = read_file(p)
            if content is not None:
                seen_paths.add(p)
                results.append({"path": p, "content": content, "source": source})

    # 1. Files mentioned in the question
    for ref in extract_paths_from_question(question):
        try_add(ref, "question")

    # 2. Currently open file in code editor
    _CODE_EDITORS = {"Code", "VSCode", "Visual Studio Code", "Xcode",
                     "PyCharm", "Sublime Text", "Cursor", "Zed", "Nova"}
    if app_name in _CODE_EDITORS:
        fname = extract_editor_filename(window_title, app_name)
        if fname:
            try_add(fname, "editor")

    return results
