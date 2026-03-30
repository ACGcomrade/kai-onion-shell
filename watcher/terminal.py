"""
TerminalSniffer — reads recent shell history.
Supports zsh, bash, PowerShell (Windows).
"""
from __future__ import annotations
import platform
import re
from pathlib import Path

_ZSH_TS = re.compile(r"^:\s*\d+:\d+;(.+)$")
_MAX = 20


def _history_files() -> list[Path]:
    candidates = [
        Path.home() / ".zsh_history",
        Path.home() / ".bash_history",
        Path.home() / ".history",
    ]
    if platform.system() == "Windows":
        candidates.append(
            Path.home() / "AppData/Roaming/Microsoft/Windows/PowerShell"
              / "PSReadLine/ConsoleHost_history.txt"
        )
    return [p for p in candidates if p.exists()]


def _parse_line(line: str) -> str:
    m = _ZSH_TS.match(line)
    return (m.group(1) if m else line).strip()


def get_recent_commands(privacy: str = "standard") -> list[str]:
    from .privacy import redact_command
    for path in _history_files():
        try:
            lines = path.read_text(errors="replace").splitlines()[-100:]
            cmds = [_parse_line(l) for l in lines if l.strip()]
            # Deduplicate preserving order
            seen: set[str] = set()
            unique = []
            for c in cmds:
                if c and c not in seen:
                    seen.add(c)
                    unique.append(redact_command(c, privacy=privacy))
            return unique[-_MAX:]
        except Exception:
            pass
    return []
