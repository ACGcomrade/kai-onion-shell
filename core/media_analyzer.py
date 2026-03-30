"""
media_analyzer.py — Host-side media file analysis (images, videos, audio).

Runs ONLY on the watcher (host) side, never inside Docker.
Results are stored in the file_descriptions table and read by web_app via DB.

Vision model priority (same as screen.py):
  1. Claude API (anthropic_api_key set)
  2. Ollama HQ vision model (ollama_vision_model_hq)
  3. Ollama moondream (ollama_vision_model fallback)

All optional dependencies degrade gracefully:
  - Pillow: image loading/resizing (falls back to raw bytes for JPEG/PNG)
  - cv2: video keyframe extraction (returns None if unavailable)
  - faster-whisper: audio transcription (skipped if unavailable)
  - ffmpeg: audio extraction from video (skipped if unavailable)
"""
from __future__ import annotations
import base64
import io
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("onion.media_analyzer")

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".bmp", ".tiff", ".tif", ".heic", ".heif",
    ".avif", ".ico",
}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".wma"}

_MAX_SIZE = 50 * 1024 * 1024   # 50 MB max for media files
_MAX_DIM = 1280                 # max dimension for image encoding


def is_media_file(path: Path) -> Optional[str]:
    """Return 'image' | 'video' | 'audio' | None based on file extension."""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


# ── Shared vision LLM helper ─────────────────────────────────────────────────

def _call_vision_llm(b64_jpeg: str, prompt: str, conf) -> Optional[str]:
    """Send a base64-encoded JPEG to the best available vision model."""
    try:
        import litellm
        litellm.suppress_debug_info = True
    except ImportError:
        log.debug("litellm not available — cannot call vision LLM")
        return None

    providers = conf.providers

    # 1. Claude API
    try:
        if getattr(providers, "anthropic_api_key", ""):
            model = providers.anthropic_model or "claude-haiku-4-5-20251001"
            resp = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64_jpeg}"}},
                ]}],
                max_tokens=500, temperature=0.1,
                api_key=providers.anthropic_api_key,
            )
            return resp.choices[0].message.content.strip()
    except Exception as e:
        log.debug("Claude vision failed: %s", e)

    # 2. Ollama HQ vision model
    try:
        hq = getattr(providers, "ollama_vision_model_hq", "")
        base_model = hq or getattr(providers, "ollama_vision_model", "llava:7b")
        if base_model:
            resp = litellm.completion(
                model=f"ollama/{base_model}",
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64_jpeg}"}},
                ]}],
                max_tokens=500, temperature=0.1,
                api_base=providers.ollama_url,
            )
            return resp.choices[0].message.content.strip()
    except Exception as e:
        log.debug("Ollama HQ vision failed: %s", e)

    # 3. moondream fallback
    try:
        vm = getattr(providers, "ollama_vision_model", "moondream")
        hq_used = hq or getattr(providers, "ollama_vision_model", "llava:7b")
        if vm and vm != hq_used:
            resp = litellm.completion(
                model=f"ollama/{vm}",
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64_jpeg}"}},
                ]}],
                max_tokens=300, temperature=0.1,
                api_base=providers.ollama_url,
            )
            return resp.choices[0].message.content.strip()
    except Exception as e:
        log.debug("moondream fallback failed: %s", e)

    return None


# ── Image analysis ────────────────────────────────────────────────────────────

def _load_image_as_jpeg_b64(path: Path) -> Optional[str]:
    """Load image, resize if needed, return base64 JPEG. Returns None on failure."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > _MAX_SIZE:
        log.info("Image too large (>50MB), skipping: %s", path.name)
        return None

    # Try Pillow first
    try:
        from PIL import Image
        img = Image.open(path)
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > _MAX_DIM:
            scale = _MAX_DIM / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        pass
    except Exception as e:
        log.debug("Pillow failed for %s: %s", path.name, e)

    # Fallback: raw bytes for JPEG/PNG/GIF/WebP
    try:
        ext = path.suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            data = path.read_bytes()
            return base64.b64encode(data).decode()
        log.info("Cannot load %s without Pillow installed", path.suffix)
    except Exception as e:
        log.debug("Raw read failed for %s: %s", path.name, e)

    return None


def analyze_image(path: Path, conf) -> Optional[dict]:
    """
    Analyze an image file with the vision LLM.
    Returns {"description": str, "summary": str, "file_type": "image"} or None.
    """
    b64 = _load_image_as_jpeg_b64(path)
    if not b64:
        return None

    prompt = (
        f"Describe the content of this image file in detail. File name: {path.name}\n"
        "Be specific about:\n"
        "- What objects, people, animals, text, or scenes are visible\n"
        "- Colors, composition, and overall style\n"
        "- Any readable text in the image\n"
        "- What you think this image is about or its likely source/context\n"
        "First give a detailed description (3-5 sentences), then on a new line starting with "
        "SUMMARY: give a one-sentence summary."
    )

    result = _call_vision_llm(b64, prompt, conf)
    if not result:
        return None

    # Parse description and summary
    if "SUMMARY:" in result:
        parts = result.split("SUMMARY:", 1)
        description = parts[0].strip()
        summary = parts[1].strip()
    else:
        description = result.strip()
        # Auto-generate a short summary from the first sentence
        first_sentence = description.split(".")[0].strip()
        summary = first_sentence[:120] if first_sentence else description[:120]

    log.info("Image analyzed: %s (%d chars)", path.name, len(description))
    return {"description": description, "summary": summary, "file_type": "image"}


# ── Audio transcription helper ────────────────────────────────────────────────

def _transcribe_audio_file(audio_path: str,
                            language: str = "") -> Optional[dict]:
    """
    Transcribe an audio file using faster-whisper.
    Returns {"transcript": str, "duration_s": float, "language": str} or None.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.debug("faster-whisper not available — skipping audio transcription")
        return None

    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        kw = {}
        if language:
            kw["language"] = language
        segments, info = model.transcribe(audio_path, beam_size=5, **kw)
        transcript = " ".join(seg.text.strip() for seg in segments).strip()
        duration_s = getattr(info, "duration", 0.0) or 0.0
        lang = getattr(info, "language", language) or language
        log.info("Transcribed %s: %d chars (%.1fs)", audio_path, len(transcript), duration_s)
        return {"transcript": transcript, "duration_s": duration_s, "language": lang}
    except Exception as e:
        log.debug("faster-whisper transcription failed for %s: %s", audio_path, e)
        return None


# ── Video analysis ────────────────────────────────────────────────────────────

def analyze_video(path: Path, conf) -> Optional[dict]:
    """
    Analyze a video file: extract keyframes via cv2 + optional audio transcript.
    Returns {"description": str, "summary": str, "transcript": str,
             "duration_s": float, "file_type": "video"} or None.
    """
    try:
        import cv2
    except ImportError:
        log.info("cv2 not available — skipping video analysis for %s", path.name)
        return None

    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            log.debug("cv2 cannot open: %s", path)
            return None

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        duration_s = total_frames / fps if fps > 0 else 0.0

        # Extract 5 evenly-spaced keyframes
        n_frames = 5
        if total_frames < n_frames:
            n_frames = max(1, total_frames)
        frame_indices = [
            int(i * (total_frames - 1) / max(n_frames - 1, 1))
            for i in range(n_frames)
        ]

        frame_descriptions = []
        for idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret:
                continue
            # Encode frame as JPEG base64
            try:
                # Resize if needed
                h, w = frame.shape[:2]
                if max(h, w) > _MAX_DIM:
                    scale = _MAX_DIM / max(h, w)
                    new_w, new_h = int(w * scale), int(h * scale)
                    frame = cv2.resize(frame, (new_w, new_h))
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    continue
                b64 = base64.b64encode(buf.tobytes()).decode()
                ts_s = idx / fps
                prompt = (
                    f"Describe what is shown in this video frame from '{path.name}'. "
                    f"Frame timestamp: {ts_s:.1f}s. Be concise (1-2 sentences)."
                )
                desc = _call_vision_llm(b64, prompt, conf)
                if desc:
                    frame_descriptions.append(f"[{ts_s:.0f}s] {desc.strip()}")
            except Exception as e:
                log.debug("Frame encoding error at idx %d: %s", idx, e)
        cap.release()

        if not frame_descriptions:
            log.info("No frame descriptions for %s", path.name)
            return None

    except Exception as e:
        log.warning("Video analysis failed for %s: %s", path.name, e)
        return None

    # Audio transcript via ffmpeg + faster-whisper
    transcript = ""
    try:
        tmp_wav = tempfile.mktemp(suffix=".wav", prefix="onion_audio_")
        ret = subprocess.run(
            ["ffmpeg", "-i", str(path), "-vn", "-f", "wav",
             "-ar", "16000", "-ac", "1", tmp_wav, "-y"],
            capture_output=True, timeout=60
        )
        if ret.returncode == 0:
            audio_result = _transcribe_audio_file(tmp_wav)
            if audio_result:
                transcript = audio_result["transcript"]
        # Clean up temp file
        try:
            import os as _os
            _os.unlink(tmp_wav)
        except Exception:
            pass
    except FileNotFoundError:
        log.debug("ffmpeg not available — skipping audio extraction for %s", path.name)
    except Exception as e:
        log.debug("Audio extraction failed for %s: %s", path.name, e)

    # Combine frame descriptions + transcript into final description
    frames_text = "\n".join(frame_descriptions)
    description_parts = [f"Video: {path.name} ({duration_s:.1f}s)"]
    description_parts.append("Keyframes:\n" + frames_text)
    if transcript:
        description_parts.append(f"Audio transcript: {transcript[:400]}")
    description = "\n".join(description_parts)

    # Summary: first frame description
    first_desc = frame_descriptions[0] if frame_descriptions else ""
    summary = first_desc[:120] if first_desc else f"Video {path.name}"
    if transcript:
        summary += f" | Audio: {transcript[:80]}"

    log.info("Video analyzed: %s (%.1fs, %d frames, %d transcript chars)",
             path.name, duration_s, len(frame_descriptions), len(transcript))
    return {
        "description": description,
        "summary": summary,
        "transcript": transcript,
        "duration_s": duration_s,
        "file_type": "video",
    }


# ── Audio analysis ────────────────────────────────────────────────────────────

def analyze_audio(path: Path, conf) -> Optional[dict]:
    """
    Analyze an audio file via faster-whisper transcription.
    Returns {"description": str, "summary": str, "transcript": str,
             "duration_s": float, "file_type": "audio"} or None.
    """
    result = _transcribe_audio_file(str(path))
    if not result:
        return None

    transcript = result["transcript"]
    duration_s = result["duration_s"]

    if not transcript:
        log.info("Empty transcript for %s", path.name)
        return None

    description = (
        f"Audio file: {path.name} ({duration_s:.1f}s)\n"
        f"Transcript: {transcript}"
    )
    summary = transcript[:120] if transcript else f"Audio {path.name}"

    log.info("Audio analyzed: %s (%.1fs, %d chars)", path.name, duration_s, len(transcript))
    return {
        "description": description,
        "summary": summary,
        "transcript": transcript,
        "duration_s": duration_s,
        "file_type": "audio",
    }


# ── Unified entry point ───────────────────────────────────────────────────────

def analyze_image_or_video(path: Path, conf) -> Optional[dict]:
    """
    Dispatch to the right analyzer based on file type.
    Returns the result dict or None if analysis failed / unsupported type.
    """
    ftype = is_media_file(path)
    if ftype == "image":
        return analyze_image(path, conf)
    elif ftype == "video":
        return analyze_video(path, conf)
    elif ftype == "audio":
        return analyze_audio(path, conf)
    return None
