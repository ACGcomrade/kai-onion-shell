"""
Privacy filtering — applied before storing ANY data.

Three modes (set in config.privacy):
  strict   : blocked apps pause ALL capture + aggressive redaction
  standard : redact common sensitive patterns
  minimal  : no filtering (user's responsibility)
"""
from __future__ import annotations
import re

_PATTERNS_STANDARD = [
    re.compile(r"-----BEGIN [\w ]* PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"(password|passwd|pwd|secret)\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"(api[_\-]?key|token|bearer)\s*[=:]\s*[A-Za-z0-9+/\-_]{20,}", re.IGNORECASE),
    re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"),   # credit card
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                              # SSN
]

_BLOCKED_APPS_STRICT = {
    "1Password", "Keychain Access", "LastPass", "Bitwarden",
    "KeePass", "Dashlane", "Enpass", "NordPass",
}


def redact_text(text: str, privacy: str = "standard") -> str:
    """Replace sensitive patterns with [REDACTED]. Returns original if privacy=minimal."""
    if privacy == "minimal" or not text:
        return text
    for pat in _PATTERNS_STANDARD:
        text = pat.sub("[REDACTED]", text)
    return text


def is_blocked_app(app_name: str, privacy: str = "standard") -> bool:
    """Return True if we should pause ALL capture (strict mode + blocked app)."""
    if privacy != "strict":
        return False
    return app_name in _BLOCKED_APPS_STRICT


def redact_command(cmd: str, privacy: str = "standard") -> str:
    """Redact sensitive flags from shell commands (e.g. -p password)."""
    if privacy == "minimal":
        return cmd
    # -p <arg>, --password <arg>, --token <arg>
    cmd = re.sub(
        r"(-p|--password|--passwd|--token|--secret)\s+\S+",
        r"\1 [REDACTED]",
        cmd, flags=re.IGNORECASE
    )
    return cmd
