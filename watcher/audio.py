"""
audio.py — Microphone capture + Voice Activity Detection + local transcription.

All dependencies are optional. If any are missing or no microphone is detected,
every function degrades gracefully (logs a warning, returns empty results).

Capture model:
  - sounddevice streams 16kHz mono audio in 10ms chunks
  - WebRTC VAD classifies each chunk as speech or silence
  - Speech chunks accumulate into a segment (max 30s)
  - Silence > 1.5s triggers transcription of the accumulated segment
  - faster-whisper runs locally (CPU) to produce transcript text
  - Raw audio is NEVER stored — only the transcript text

Platform: macOS, Linux, Windows (requires portaudio + sounddevice)
On Windows: install webrtcvad-wheels instead of webrtcvad
"""
from __future__ import annotations
import asyncio
import io
import logging
import time
import platform
from typing import Optional

log = logging.getLogger("onion.audio")

# ── Optional dependency probing ────────────────────────────────────────────

_SOUNDDEVICE = False
_VAD = False
_WHISPER = False
_MIC_AVAILABLE = False

try:
    import sounddevice as sd
    import numpy as np
    # Verify at least one input device exists
    devices = sd.query_devices()
    has_input = any(d["max_input_channels"] > 0 for d in devices)
    if has_input:
        _SOUNDDEVICE = True
        _MIC_AVAILABLE = True
        log.info("Audio: sounddevice OK, microphone available")
    else:
        log.info("Audio: sounddevice installed but no input device found — mic capture disabled")
except ImportError:
    log.info("Audio: sounddevice not installed — mic capture disabled (pip install sounddevice)")
except Exception as e:
    log.info("Audio: sounddevice error (%s) — mic capture disabled", e)

try:
    import webrtcvad
    _VAD = True
    log.info("Audio: WebRTC VAD available")
except ImportError:
    try:
        # Windows precompiled wheels
        import webrtcvad_wheels as webrtcvad  # type: ignore
        _VAD = True
        log.info("Audio: WebRTC VAD (wheels) available")
    except ImportError:
        log.info("Audio: webrtcvad not installed — using energy-based VAD fallback")

try:
    from faster_whisper import WhisperModel as _WhisperModel
    _WHISPER = True
    log.info("Audio: faster-whisper available")
except ImportError:
    log.info("Audio: faster-whisper not installed — transcription disabled (pip install faster-whisper)")


def is_available() -> bool:
    """Return True if mic capture + transcription are both functional."""
    return _MIC_AVAILABLE and _WHISPER


# ── Whisper model (lazy-loaded, singleton) ─────────────────────────────────

_whisper_model: Optional[object] = None
_whisper_model_size: str = ""


def _get_whisper(model_size: str = "base") -> Optional[object]:
    global _whisper_model, _whisper_model_size
    if not _WHISPER:
        return None
    if _whisper_model is None or _whisper_model_size != model_size:
        try:
            log.info("Audio: loading faster-whisper model '%s'...", model_size)
            _whisper_model = _WhisperModel(
                model_size,
                device="cpu",
                compute_type="int8",   # fastest CPU inference
            )
            _whisper_model_size = model_size
            log.info("Audio: faster-whisper '%s' loaded", model_size)
        except Exception as e:
            log.warning("Audio: failed to load whisper model: %s", e)
            return None
    return _whisper_model


# ── Energy-based VAD fallback (no webrtcvad needed) ───────────────────────

def _energy_vad(samples: "np.ndarray", threshold: float = 0.01) -> bool:
    """Simple RMS energy check. Returns True if audio chunk is likely speech."""
    import numpy as np
    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
    return rms > threshold


# ── Core capture + transcription ──────────────────────────────────────────

class AudioCapture:
    """
    Captures microphone audio continuously, uses VAD to detect speech segments,
    and transcribes them with faster-whisper.

    Usage:
        cap = AudioCapture(language="", model_size="base")
        async for result in cap.segments():
            print(result)  # {"transcript": "...", "duration_s": 5.2, "language": "en"}
    """

    SAMPLE_RATE = 16000          # Hz — whisper native rate
    CHUNK_MS    = 30             # ms per VAD frame (10/20/30 are valid for webrtcvad)
    CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000   # 480 samples

    MAX_SEGMENT_S   = 30.0       # max segment length before forcing transcription
    SILENCE_END_S   = 1.5        # silence duration that ends a segment
    MIN_SPEECH_S    = 0.5        # ignore segments shorter than this

    def __init__(self, language: str = "", model_size: str = "base",
                 vad_aggressiveness: int = 2):
        self.language = language or None   # None = auto-detect
        self.model_size = model_size
        self._vad_aggressiveness = vad_aggressiveness
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    def _make_vad(self) -> Optional[object]:
        if not _VAD:
            return None
        try:
            vad = webrtcvad.Vad(self._vad_aggressiveness)
            return vad
        except Exception:
            return None

    def _transcribe(self, audio_bytes: bytes, duration_s: float) -> Optional[dict]:
        """Run faster-whisper on raw PCM bytes. Returns transcript dict or None."""
        model = _get_whisper(self.model_size)
        if not model:
            return None
        try:
            import numpy as np
            samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            segments, info = model.transcribe(
                samples,
                language=self.language,
                beam_size=3,
                vad_filter=True,          # built-in whisper VAD for cleanup
                vad_parameters={"min_silence_duration_ms": 300},
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if not text:
                return None
            return {
                "transcript": text,
                "duration_s": round(duration_s, 1),
                "language": info.language or "",
                "confidence": round(info.language_probability or 0.0, 2),
            }
        except Exception as e:
            log.warning("Audio: transcription failed: %s", e)
            return None

    def _stream_callback(self, indata, frames, time_info, status):
        """sounddevice callback — runs in a separate thread."""
        if status:
            log.debug("Audio: stream status %s", status)
        # Copy raw bytes into queue (non-blocking)
        chunk = bytes(indata)
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            pass   # drop chunk if queue is full (shouldn't happen with reasonable settings)

    async def segments(self):
        """
        Async generator that yields transcript dicts for each detected speech segment.
        Runs until stop() is called.
        """
        if not _SOUNDDEVICE or not _MIC_AVAILABLE:
            log.info("Audio: no microphone — segment generator yields nothing")
            return
        if not _WHISPER:
            log.info("Audio: faster-whisper not available — segment generator yields nothing")
            return

        self._running = True
        vad = self._make_vad()
        loop = asyncio.get_event_loop()

        speech_buf: list[bytes] = []
        silence_frames = 0
        silence_end_frames = int(self.SILENCE_END_S * 1000 / self.CHUNK_MS)
        max_frames = int(self.MAX_SEGMENT_S * 1000 / self.CHUNK_MS)

        try:
            stream = sd.RawInputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=self.CHUNK_SAMPLES,
                dtype="int16",
                channels=1,
                callback=self._stream_callback,
            )
            with stream:
                log.info("Audio: microphone stream started (rate=%dHz, chunk=%dms)",
                         self.SAMPLE_RATE, self.CHUNK_MS)
                while self._running:
                    # Fetch chunks from queue
                    try:
                        chunk = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    # VAD decision
                    import numpy as np
                    samples = np.frombuffer(chunk, dtype=np.int16)
                    if vad:
                        try:
                            is_speech = vad.is_speech(chunk, self.SAMPLE_RATE)
                        except Exception:
                            is_speech = _energy_vad(samples)
                    else:
                        is_speech = _energy_vad(samples)

                    if is_speech:
                        speech_buf.append(chunk)
                        silence_frames = 0
                    elif speech_buf:
                        silence_frames += 1
                        speech_buf.append(chunk)   # include trailing silence for natural cut
                        if silence_frames >= silence_end_frames or len(speech_buf) >= max_frames:
                            # Segment done — transcribe
                            duration_s = len(speech_buf) * self.CHUNK_MS / 1000.0
                            if duration_s >= self.MIN_SPEECH_S:
                                audio_bytes = b"".join(speech_buf)
                                # Run in executor so we don't block the event loop
                                result = await loop.run_in_executor(
                                    None, self._transcribe, audio_bytes, duration_s
                                )
                                if result:
                                    yield result
                            speech_buf.clear()
                            silence_frames = 0

        except Exception as e:
            log.warning("Audio: stream error: %s", e)
        finally:
            self._running = False
            log.info("Audio: microphone stream stopped")

    def stop(self):
        self._running = False


# ── Simple one-shot capture (for ask_trigger) ─────────────────────────────

async def capture_segment(duration_s: float = 10.0, language: str = "",
                          model_size: str = "base") -> Optional[dict]:
    """
    Record a fixed-duration audio segment and transcribe it.
    Used at ask_trigger time to capture what the user just said.
    Returns None if no mic/whisper available or no speech detected.
    """
    if not _SOUNDDEVICE or not _MIC_AVAILABLE or not _WHISPER:
        return None

    import numpy as np
    try:
        log.info("Audio: recording %gs segment...", duration_s)
        samples = sd.rec(
            int(duration_s * 16000),
            samplerate=16000,
            channels=1,
            dtype="int16",
        )
        await asyncio.get_event_loop().run_in_executor(None, sd.wait)
        audio_bytes = samples.tobytes()
        cap = AudioCapture(language=language, model_size=model_size)
        result = await asyncio.get_event_loop().run_in_executor(
            None, cap._transcribe, audio_bytes, duration_s
        )
        return result
    except Exception as e:
        log.warning("Audio: capture_segment failed: %s", e)
        return None
