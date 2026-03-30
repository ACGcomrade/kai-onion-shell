"""
input_tracker.py — Keyboard typing content + mouse click/scroll tracking.

Uses pynput (cross-platform) to capture:
  - Keyboard: accumulates typed text between pauses; logs content segments
  - Mouse: click coordinates (app-context only), scroll activity

Privacy:
  - Password fields: impossible to distinguish at OS level, but content is
    suppressed if active app is in privacy.BLOCKED_APPS or password-manager apps
  - Typed text is buffered and flushed only after a typing pause (> FLUSH_IDLE_S)
  - Raw keystrokes are NEVER stored individually — only assembled text segments
  - Special keys (arrows, function keys) are recorded as metadata only, not content
  - Mouse coordinates are stored as relative zones (left/center/right × top/mid/bottom)
    to avoid pixel-precise surveillance

All dependencies are optional. If pynput is absent, everything degrades gracefully.

Install: pip install pynput
Platform: macOS (requires Accessibility permission), Windows, Linux (X11/Wayland)
"""
from __future__ import annotations
import asyncio
import logging
import string
import threading
import time
from typing import Optional, Callable

log = logging.getLogger("onion.input")

# ── Optional dependency probe ────────────────────────────────────────────────

_PYNPUT = False

try:
    from pynput import keyboard as _kb, mouse as _mouse
    _PYNPUT = True
    log.info("Input tracker: pynput available")
except ImportError:
    log.info("Input tracker: pynput not installed — keyboard/mouse tracking disabled "
             "(pip install pynput)")
except Exception as e:
    log.info("Input tracker: pynput failed to load (%s) — disabled", e)


def is_available() -> bool:
    return _PYNPUT


# ── Constants ────────────────────────────────────────────────────────────────

FLUSH_IDLE_S = 3.0       # flush typed buffer after this many seconds of silence
MIN_TEXT_LEN  = 5        # ignore segments shorter than this (accidental keypresses)
MAX_SEGMENT_S = 60.0     # force-flush after max segment duration


# ── Keyboard tracker ─────────────────────────────────────────────────────────

class InputTracker:
    """
    Tracks keyboard typing and mouse interactions.

    Usage:
        tracker = InputTracker(on_text=cb_text, on_mouse=cb_mouse)
        tracker.start()
        # ... later ...
        tracker.stop()

    Callbacks (called from background thread — must be thread-safe):
        on_text(text: str, app_name: str)  — called with assembled typed segment
        on_mouse(action: str, zone: str)   — action='click'|'scroll', zone='top-left' etc.
    """

    def __init__(self,
                 on_text: Optional[Callable[[str, str], None]] = None,
                 on_mouse: Optional[Callable[[str, str], None]] = None,
                 get_app_name: Optional[Callable[[], str]] = None,
                 get_privacy_mode: Optional[Callable[[], str]] = None):
        self._on_text = on_text
        self._on_mouse = on_mouse
        self._get_app = get_app_name or (lambda: "")
        self._get_privacy = get_privacy_mode or (lambda: "standard")

        self._buf: list[str] = []
        self._buf_app: str = ""
        self._last_key_ts: float = 0.0
        self._segment_start_ts: float = 0.0

        self._kb_listener = None
        self._mouse_listener = None
        self._flush_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    # ── Privacy check ────────────────────────────────────────────────────────

    def _is_suppressed(self) -> bool:
        """Return True if current app should suppress keyboard capture."""
        privacy_mode = self._get_privacy()
        app = self._get_app().lower()
        BLOCKED = {
            "1password", "keychain access", "lastpass", "bitwarden",
            "keepass", "dashlane", "keychain", "gnome-keyring",
        }
        if any(b in app for b in BLOCKED):
            return True
        if privacy_mode == "strict" and any(b in app for b in BLOCKED):
            return True
        return False

    # ── Buffer management ────────────────────────────────────────────────────

    def _flush(self):
        """Emit accumulated text if above minimum length."""
        with self._lock:
            if not self._buf:
                return
            text = "".join(self._buf).strip()
            app = self._buf_app
            self._buf.clear()
            self._segment_start_ts = 0.0

        if len(text) >= MIN_TEXT_LEN and self._on_text:
            self._on_text(text, app)

    def _flush_loop(self):
        """Background thread: flush buffer after typing pause."""
        while self._running:
            time.sleep(0.5)
            with self._lock:
                if not self._buf:
                    continue
                elapsed = time.time() - self._last_key_ts
                too_long = (time.time() - self._segment_start_ts) > MAX_SEGMENT_S
            if elapsed >= FLUSH_IDLE_S or too_long:
                self._flush()

    # ── Keyboard events ──────────────────────────────────────────────────────

    def _on_key_press(self, key):
        if not self._running:
            return
        if self._is_suppressed():
            return

        now = time.time()
        app = self._get_app()

        with self._lock:
            # If app changed since buffer started, flush first
            if self._buf and self._buf_app != app:
                old_buf = "".join(self._buf).strip()
                old_app = self._buf_app
                self._buf.clear()
                if len(old_buf) >= MIN_TEXT_LEN and self._on_text:
                    threading.Thread(
                        target=self._on_text, args=(old_buf, old_app), daemon=True
                    ).start()

            # Record printable characters
            try:
                ch = key.char
                if ch and ch in string.printable:
                    if not self._buf:
                        self._segment_start_ts = now
                        self._buf_app = app
                    self._buf.append(ch)
                    self._last_key_ts = now
            except AttributeError:
                # Special key (Enter, Backspace, arrows, etc.)
                # Enter → treat as segment boundary
                if key == _kb.Key.enter:
                    self._flush()
                # Backspace → remove last char if any
                elif key == _kb.Key.backspace:
                    if self._buf:
                        self._buf.pop()
                    self._last_key_ts = now
                # Space → record as space
                elif key == _kb.Key.space:
                    if not self._buf:
                        self._segment_start_ts = now
                        self._buf_app = app
                    self._buf.append(" ")
                    self._last_key_ts = now

    # ── Mouse events ─────────────────────────────────────────────────────────

    @staticmethod
    def _screen_zone(x: float, y: float) -> str:
        """Convert pixel coordinates to coarse 3×3 zone (privacy-preserving)."""
        try:
            import subprocess, platform
            if platform.system() == "Darwin":
                out = subprocess.check_output(
                    ["system_profiler", "SPDisplaysDataType"],
                    stderr=subprocess.DEVNULL, timeout=2
                ).decode()
                # Extract resolution — crude but good enough
                for line in out.splitlines():
                    if "Resolution" in line:
                        parts = line.split(":")[-1].strip().split(" x ")
                        if len(parts) == 2:
                            w, h = float(parts[0].split()[0]), float(parts[1].split()[0])
                            col = "left" if x < w/3 else ("right" if x > 2*w/3 else "center")
                            row = "top"  if y < h/3 else ("bottom" if y > 2*h/3 else "mid")
                            return f"{row}-{col}"
        except Exception:
            pass
        return "unknown"

    def _on_click(self, x, y, button, pressed):
        if not self._running or not pressed:
            return
        if not self._on_mouse:
            return
        zone = self._screen_zone(x, y)
        self._on_mouse("click", zone)

    def _on_scroll(self, x, y, dx, dy):
        if not self._running:
            return
        if not self._on_mouse:
            return
        direction = "up" if dy > 0 else "down"
        self._on_mouse(f"scroll-{direction}", "")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if not _PYNPUT:
            return
        self._running = True
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

        self._kb_listener = _kb.Listener(on_press=self._on_key_press)
        self._kb_listener.start()

        self._mouse_listener = _mouse.Listener(
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._mouse_listener.start()
        log.info("Input tracker started (keyboard + mouse)")

    def stop(self):
        self._running = False
        self._flush()
        if self._kb_listener:
            self._kb_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()
        log.info("Input tracker stopped")
