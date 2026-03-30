"""
camera.py — Webcam frame capture + motion detection + vision description.

All dependencies are optional. If opencv or the camera hardware is absent,
every function degrades gracefully (logs a warning, returns empty results).

Capture model:
  - cv2.VideoCapture grabs a single frame periodically (not continuous stream)
  - Frame-difference check (grayscale mean-absolute-diff) detects meaningful change
  - Only if motion_score >= threshold: send frame to vision LLM for description
  - Raw frames are NEVER stored — only the text description
  - Source tag 'camera' distinguishes from 'screen' in vision_snapshots table

Presence detection:
  - Description is checked for person-presence keywords
  - Caller (daemon) updates _presence_confirmed based on result

Platform: macOS, Linux, Windows — cv2.VideoCapture is cross-platform
"""
from __future__ import annotations
import asyncio
import base64
import logging
import os
import tempfile
import time
from typing import Optional

log = logging.getLogger("onion.camera")

# ── Optional dependency probing ────────────────────────────────────────────

_CV2 = False
_CAMERA_INDEX: Optional[int] = None   # first available camera device index

try:
    import cv2 as _cv2
    import numpy as _np
    _CV2 = True
    # Probe for camera (index 0 is the built-in webcam on most systems)
    for _idx in range(3):   # try indices 0, 1, 2
        _cap = _cv2.VideoCapture(_idx)
        if _cap.isOpened():
            _ok, _test_frame = _cap.read()
            _cap.release()
            if _ok and _test_frame is not None:
                _CAMERA_INDEX = _idx
                log.info("Camera: found at index %d", _idx)
                break
    if _CAMERA_INDEX is None:
        log.info("Camera: cv2 installed but no camera detected — camera capture disabled")
except ImportError:
    log.info("Camera: opencv-python-headless not installed — camera capture disabled "
             "(pip install opencv-python-headless)")
except Exception as e:
    log.info("Camera: probe failed (%s) — camera capture disabled", e)


def is_available() -> bool:
    """Return True if a camera was detected and cv2 is installed."""
    return _CV2 and _CAMERA_INDEX is not None


# ── Frame capture ──────────────────────────────────────────────────────────

def _grab_frame() -> Optional[bytes]:
    """
    Capture one JPEG frame from the webcam.
    Returns raw JPEG bytes, or None on failure.
    Raw bytes are passed to LLM immediately and never written to disk.
    """
    if not is_available():
        return None
    try:
        cap = _cv2.VideoCapture(_CAMERA_INDEX)
        cap.set(_cv2.CAP_PROP_FRAME_WIDTH, 640)    # don't need full resolution
        cap.set(_cv2.CAP_PROP_FRAME_HEIGHT, 480)
        # Skip a few frames to let the camera warm up (auto-exposure)
        for _ in range(3):
            cap.grab()
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None
        ok, buf = _cv2.imencode(".jpg", frame, [_cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return None
        return bytes(buf)
    except Exception as e:
        log.debug("Camera: grab_frame error: %s", e)
        return None


# ── Motion detection ───────────────────────────────────────────────────────

_prev_gray: Optional["_np.ndarray"] = None


def _motion_score(jpeg_bytes: bytes) -> float:
    """
    Compare current frame to previous frame using grayscale mean absolute diff.
    Returns 0.0–1.0.  > 0.05 is considered meaningful change.
    Updates the stored previous frame.
    """
    global _prev_gray
    if not _CV2:
        return 1.0   # no comparison possible → always process
    try:
        arr = _np.frombuffer(jpeg_bytes, dtype=_np.uint8)
        frame = _cv2.imdecode(arr, _cv2.IMREAD_GRAYSCALE)
        if frame is None:
            return 1.0
        # Resize to small thumbnail for cheap comparison
        thumb = _cv2.resize(frame, (80, 60))
        if _prev_gray is None or _prev_gray.shape != thumb.shape:
            _prev_gray = thumb
            return 1.0   # first frame — always describe
        diff = _cv2.absdiff(_prev_gray, thumb)
        score = float(_np.mean(diff)) / 255.0
        _prev_gray = thumb
        return score
    except Exception:
        return 1.0


# ── Person/presence detection from description ─────────────────────────────

_PRESENCE_KEYWORDS = {
    "person", "people", "human", "face", "man", "woman", "user", "someone",
    "sitting", "standing", "looking", "typing", "working",
    # Chinese equivalents (llava may output either)
    "人", "用户", "坐", "站", "看", "打字",
}


def has_person(description: str) -> bool:
    """Return True if the camera description suggests a person is in frame."""
    low = description.lower()
    return any(kw in low for kw in _PRESENCE_KEYWORDS)


# ── Vision description ─────────────────────────────────────────────────────

async def describe_frame(conf, app_name: str = "", window_title: str = "") -> Optional[str]:
    """
    Capture one webcam frame and describe it using the HQ vision model.
    Returns description text, or None if camera unavailable / frame unchanged.

    motion_threshold: skip LLM call if scene hasn't changed significantly.
    """
    if not is_available():
        return None

    loop = asyncio.get_event_loop()
    jpeg_bytes = await loop.run_in_executor(None, _grab_frame)
    if not jpeg_bytes:
        return None

    # Motion check — skip if scene is static
    threshold = getattr(conf, "sensors", None)
    threshold = (threshold.camera_motion_threshold
                 if threshold else 0.05)
    score = _motion_score(jpeg_bytes)
    if score < threshold:
        log.debug("Camera: motion_score=%.3f < %.3f — skipping LLM", score, threshold)
        return None

    b64 = base64.b64encode(jpeg_bytes).decode()

    prompt = (
        "This is a webcam frame (not a computer screen). Describe what you see:\n"
        "- Is there a person present? If yes, describe what they appear to be doing "
        "(sitting, standing, typing, looking at screen, talking, etc.)\n"
        "- How many people are visible?\n"
        "- Any notable physical objects on the desk or in the background?\n"
        "- Overall setting (home office, open office, dark room, etc.)\n"
        "Keep it to 2-3 sentences. Do NOT describe the monitor or computer UI.\n"
    )
    if app_name:
        prompt += f"Context: active app is {app_name}."

    def _call():
        try:
            import litellm
            litellm.suppress_debug_info = True
            # Try HQ model first (Claude API → Ollama HQ → moondream fallback)
            providers = conf.providers

            # Claude API
            if getattr(providers, "anthropic_api_key", ""):
                resp = litellm.completion(
                    model=providers.anthropic_model or "claude-haiku-4-5-20251001",
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    max_tokens=200, temperature=0.1,
                    api_key=providers.anthropic_api_key,
                )
                return resp.choices[0].message.content.strip()
        except Exception:
            pass

        try:
            # Ollama HQ model
            hq = getattr(conf.providers, "ollama_vision_model_hq", "")
            if hq:
                resp = litellm.completion(
                    model=f"ollama/{hq}",
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    max_tokens=200, temperature=0.1,
                    api_base=conf.providers.ollama_url,
                )
                return resp.choices[0].message.content.strip()
        except Exception:
            pass

        try:
            # Moondream fallback
            vm = getattr(conf.providers, "ollama_vision_model", "moondream")
            if vm:
                resp = litellm.completion(
                    model=f"ollama/{vm}",
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    max_tokens=200, temperature=0.1,
                    api_base=conf.providers.ollama_url,
                )
                return resp.choices[0].message.content.strip()
        except Exception as e:
            log.debug("Camera: all vision models failed: %s", e)

        return None

    result = await loop.run_in_executor(None, _call)
    log.debug("Camera: motion=%.3f, desc_len=%s", score,
              len(result) if result else 0)
    return result
