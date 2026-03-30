"""
watcher/reader.py — Reader layer.

Scans configured local directories for files, stores path references
(NOT copies) in the reader_refs table. Uses LLM to generate a short
description + summary for each file (same vision/text pipeline as media_analyzer).

Key behaviours:
  - scan_directories(): walk watched dirs, upsert reader_refs for new/changed files
  - ping_refs(): verify existing refs still exist on disk
  - LLM analysis is optional (skipped if no vision model available)
  - Supports image, video, audio, text, PDF, and generic file types
"""
from __future__ import annotations
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("onion.reader")

# Directories to watch (expanded at runtime)
DEFAULT_WATCH_DIRS = [
    "~/Desktop",
    "~/Downloads",
    "~/Documents",
    "~/Pictures",
]

# File extensions to index
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif",
               ".heic", ".heif", ".avif"}
VIDEO_EXTS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
AUDIO_EXTS  = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".wma"}
TEXT_EXTS   = {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
               ".toml", ".csv", ".html", ".css", ".sh", ".zsh", ".bash",
               ".c", ".cpp", ".h", ".java", ".go", ".rs", ".rb", ".php",
               ".xml", ".log", ".ini", ".cfg", ".conf"}
PDF_EXTS    = {".pdf"}
DOC_EXTS    = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pages",
               ".numbers", ".key"}

# Max file size to attempt LLM analysis
MAX_ANALYZE_BYTES = 50 * 1024 * 1024   # 50 MB

# Re-analyze if file has changed or analysis is older than this
REANALYZE_AFTER_S = 3 * 3600           # 3 hours


def _classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:  return "image"
    if ext in VIDEO_EXTS:  return "video"
    if ext in AUDIO_EXTS:  return "audio"
    if ext in TEXT_EXTS:   return "text"
    if ext in PDF_EXTS:    return "pdf"
    if ext in DOC_EXTS:    return "document"
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        if mime.startswith("image/"):  return "image"
        if mime.startswith("video/"):  return "video"
        if mime.startswith("audio/"):  return "audio"
        if mime.startswith("text/"):   return "text"
    return "other"


def _text_summary(path: Path, max_chars: int = 500) -> str:
    """Read the first max_chars of a text file as a quick summary."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(max_chars * 2)
        # Take first 500 chars of meaningful content
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        preview = " ".join(lines)[:max_chars]
        return preview
    except Exception:
        return ""


async def _analyze_file(path: Path, file_type: str, conf) -> tuple[str, str]:
    """
    Return (description, summary) for a file using LLM if possible.
    Falls back to filesystem metadata if no LLM available.
    """
    description = ""
    summary = ""

    try:
        size = path.stat().st_size
        if size > MAX_ANALYZE_BYTES:
            summary = f"{file_type.capitalize()} file ({size // 1024 // 1024} MB)"
            return description, summary

        if file_type in ("image", "video", "audio"):
            # Use media_analyzer pipeline (already handles all three)
            from core.media_analyzer import analyze_image_or_video
            result = await analyze_image_or_video(path, conf)
            if result:
                description = result.get("description", "")
                summary = result.get("summary", "") or description[:120]

        elif file_type == "text":
            preview = _text_summary(path, max_chars=400)
            if preview:
                description = f"Text file preview: {preview}"
                summary = preview[:120]

        elif file_type == "pdf":
            # Try pdfminer or just note it's a PDF
            try:
                from pdfminer.high_level import extract_text
                text = extract_text(str(path), maxpages=2)
                if text:
                    clean = " ".join(text.split())[:500]
                    description = f"PDF content: {clean}"
                    summary = clean[:120]
            except ImportError:
                summary = f"PDF document ({path.stat().st_size // 1024} KB)"
            except Exception:
                summary = f"PDF document ({path.stat().st_size // 1024} KB)"

        elif file_type == "document":
            summary = f"{path.suffix.lstrip('.').upper()} document ({path.stat().st_size // 1024} KB)"

    except Exception as e:
        log.debug(f"Reader: analysis failed for {path}: {e}")

    if not summary:
        try:
            summary = f"{file_type.capitalize()}: {path.name} ({path.stat().st_size // 1024} KB)"
        except Exception:
            summary = f"{file_type.capitalize()}: {path.name}"

    return description, summary


async def scan_directories(store, conf, watch_dirs: list[str] | None = None,
                           max_files_per_dir: int = 500) -> int:
    """
    Walk watched directories, upsert reader_refs for new/changed files.
    Returns number of new or updated refs.
    """
    dirs = watch_dirs or DEFAULT_WATCH_DIRS
    expanded = [Path(d).expanduser() for d in dirs]

    updated = 0
    now = time.time()

    for watch_dir in expanded:
        if not watch_dir.exists():
            continue
        try:
            entries = list(watch_dir.iterdir())
        except PermissionError:
            continue

        count = 0
        for entry in entries:
            if count >= max_files_per_dir:
                break
            if entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except Exception:
                continue

            file_type = "dir" if entry.is_dir() else _classify(entry)
            if entry.is_dir():
                # For directories, just register their existence without LLM
                store.save_reader_ref(
                    path=str(entry),
                    file_type="dir",
                    size_bytes=0,
                    mtime=stat.st_mtime,
                )
                count += 1
                continue

            # Check if already indexed and unchanged
            existing = store.get_reader_ref(str(entry))
            needs_analysis = False

            if existing is None:
                needs_analysis = True
            elif existing.get("mtime") != stat.st_mtime:
                needs_analysis = True
            elif existing.get("ts_analyzed") and (now - existing["ts_analyzed"]) > REANALYZE_AFTER_S:
                needs_analysis = True

            if needs_analysis:
                description, summary = await _analyze_file(entry, file_type, conf)
                store.save_reader_ref(
                    path=str(entry),
                    file_type=file_type,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                    description=description,
                    summary=summary,
                )
                is_new = existing is None
                action = "New file" if is_new else "Updated"
                short_path = str(entry).replace(str(Path.home()), "~")
                store.log_event(
                    channel="reader",
                    summary=f"{action}: {entry.name} ({file_type})",
                    detail={"path": str(entry), "file_type": file_type,
                            "size_bytes": stat.st_size, "summary": summary[:80]},
                    category="files",
                    importance=2,
                    domain=str(watch_dir.name),
                )
                updated += 1
                log.debug(f"Reader: indexed {entry.name} ({file_type})")
            else:
                # Update last_seen ping without re-analyzing
                store.save_reader_ref(
                    path=str(entry),
                    file_type=file_type,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )

            count += 1

    return updated


def ping_refs(store) -> tuple[int, int]:
    """
    Validate all alive reader refs. Mark missing as alive=0.
    Returns (alive_count, dead_count).
    """
    alive, dead = store.ping_reader_refs()
    if dead:
        log.info(f"Reader ping: {dead} refs gone dead, {alive} still alive")
    # Delete refs that have been dead for >24h
    store.delete_dead_refs(older_than_hours=24.0)
    return alive, dead
