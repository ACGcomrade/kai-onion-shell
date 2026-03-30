"""
image_reader.py — Thin wrapper around media_analyzer for backward compatibility.

The main image/video/audio analysis logic now lives in core/media_analyzer.py.
This module re-exports the image-related constants and provides a
gather_image_context shim (kept for any legacy callers).
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from core.media_analyzer import (
    IMAGE_EXTS,
    is_media_file,
    analyze_image,
    _load_image_as_jpeg_b64,
)

log = logging.getLogger("onion.image_reader")


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def describe_image_file(path: Path, conf) -> Optional[str]:
    """Thin wrapper: analyze an image and return its description string."""
    result = analyze_image(path, conf)
    return result["description"] if result else None


def gather_image_context(
    question: str,
    window_title: str = "",
    app_name: str = "",
    fs_snapshot=None,
    conf=None,
    max_images: int = 2,
) -> list[dict]:
    """
    Legacy entry point — no longer does on-demand analysis.
    Returns empty list; media analysis is now done by the watcher background loop
    and stored in the file_descriptions DB table (read by packager).
    """
    return []
