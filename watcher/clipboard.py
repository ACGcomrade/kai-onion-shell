"""
ClipboardMonitor — cross-platform via pyperclip.
"""
from __future__ import annotations
import hashlib
import logging

log = logging.getLogger("onion.clipboard")

try:
    import pyperclip
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    log.warning("pyperclip not installed: pip install pyperclip")

_MAX = 2000


def get_clipboard(privacy: str = "standard") -> tuple[str, str]:
    """
    Returns (text[:2000], sha256_hash[:16]).
    text is redacted if it contains sensitive patterns.
    """
    from .privacy import redact_text
    if not _AVAILABLE:
        return "", ""
    try:
        text = pyperclip.paste() or ""
        if not text:
            return "", ""
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        text = redact_text(text[:_MAX], privacy=privacy)
        return text, content_hash
    except Exception as e:
        log.debug("Clipboard read error: %s", e)
        return "", ""
