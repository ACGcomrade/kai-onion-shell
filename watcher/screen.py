"""
ScreenCaptor — screenshot capture only.

OCR and vision LLM analysis have been moved to me2 (engine/analyzer/screen_analyzer.py).
Onion Shell is pure data collection: capture screenshot → save JPEG → record path in DB.
me2 reads the JPEG paths and runs OCR + vision during profile updates.
"""
from __future__ import annotations
import asyncio
import logging
import os
import tempfile

log = logging.getLogger("onion.screen")

try:
    import mss as _mss_mod
    _MSS = True
except ImportError:
    _MSS = False
    log.warning("mss not installed: pip install mss")


async def _take_screenshot() -> str | None:
    """Capture the primary monitor and return the tmp PNG path (caller must delete it)."""
    if not _MSS:
        return None
    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="onion_")
    os.close(fd)
    try:
        loop = asyncio.get_event_loop()

        def _grab():
            import mss
            import mss.tools
            with mss.mss() as sct:
                # monitors[0] = all screens combined, monitors[1+] = individual screens
                # Fall back to monitors[0] if no individual monitor is available
                # (can happen if display is asleep or configuration changed)
                monitors = sct.monitors
                monitor = monitors[1] if len(monitors) > 1 else monitors[0]
                img = sct.grab(monitor)
                mss.tools.to_png(img.rgb, img.size, output=tmp_path)

        await loop.run_in_executor(None, _grab)
        return tmp_path
    except Exception as e:
        log.warning("Screenshot failed: %s", e)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None


async def save_screenshot(jpeg_dest) -> bool:
    """
    Capture the screen and save as JPEG to jpeg_dest.
    Returns True on success. OCR and vision analysis are handled by me2.
    """
    tmp_path = await _take_screenshot()
    if not tmp_path:
        return False
    try:
        from PIL import Image as _PImg
        from pathlib import Path as _Path
        _Path(jpeg_dest).parent.mkdir(parents=True, exist_ok=True)
        _PImg.open(tmp_path).convert("RGB").save(str(jpeg_dest), "JPEG", quality=75)
        return True
    except Exception as e:
        log.debug("save_screenshot failed: %s", e)
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Stubs: OCR and vision analysis moved to me2 ──────────────────────────────

async def capture_and_ocr() -> tuple[str, float]:
    """Stub — OCR moved to me2. Returns empty so daemon falls through to save path."""
    return "", 0.0


async def capture_and_ocr_with_jpeg(jpeg_dest) -> tuple[str, float]:
    """Stub — saves JPEG only; OCR moved to me2."""
    await save_screenshot(jpeg_dest)
    return "", 0.0


async def describe_screenshot(ollama_url: str, vision_model: str,
                               app_name: str = "", window_title: str = "",
                               page_url: str = "") -> str:
    """Stub — vision LLM moved to me2."""
    return ""


async def describe_screenshot_hq(conf, app_name: str = "", window_title: str = "",
                                  page_url: str = "") -> str:
    """Stub — vision LLM moved to me2."""
    return ""
