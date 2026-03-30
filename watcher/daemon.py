"""
Onion Shell Watcher Daemon.

AdaptiveScheduler: polling intervals expand 10× when system is idle.
OCR is lazy: only triggered by window title change or periodic fallback.
Privacy-blocked apps pause all capture.
"""
from __future__ import annotations
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
import platform
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config as cfg_module
from core.store import Store
from core import media_analyzer
from core import kaidata as kaidata_mod
from core.controller import OnionController
from watcher import apps, clipboard, screen, terminal, files, privacy
from watcher import audio as audio_mod
from watcher import camera as camera_mod
from watcher import reader as reader_mod
from watcher.input_tracker import InputTracker, is_available as input_available

log = logging.getLogger("onion.daemon")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

_PAUSED = False   # set True by `onion pause`


def _extract_domain(url: str) -> str:
    """Extract bare domain from URL, e.g. 'https://www.youtube.com/watch?v=x' → 'youtube.com'"""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # Strip www. prefix
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


# ── Idle detection ────────────────────────────────────────────────────────

def _cpu_percent() -> float:
    """Current CPU usage percentage (0-100). Returns 0 if unavailable."""
    try:
        import subprocess
        if platform.system() == "Darwin":
            # top -l1 is slow; use vm_stat + ps as a fast proxy
            out = subprocess.check_output(
                ["ps", "-A", "-o", "%cpu"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode()
            total = sum(float(x) for x in out.splitlines()[1:] if x.strip().replace(".", "").isdigit())
            return min(total, 100.0)
    except Exception:
        pass
    return 0.0


def _idle_seconds() -> float:
    """Seconds since last user input. Returns 0 on unsupported platforms."""
    try:
        if platform.system() == "Darwin":
            import subprocess
            out = subprocess.check_output(
                ["ioreg", "-c", "IOHIDSystem"],
                stderr=subprocess.DEVNULL, timeout=2
            ).decode()
            for line in out.splitlines():
                if "HIDIdleTime" in line:
                    ns = int(line.split("=")[-1].strip())
                    return ns / 1e9
        elif platform.system() == "Windows":
            import ctypes
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            li = LASTINPUTINFO()
            li.cbSize = ctypes.sizeof(li)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
            millis = ctypes.windll.kernel32.GetTickCount() - li.dwTime
            return millis / 1000.0
    except Exception:
        pass
    return 0.0


def _intervals(level: int, mult: float) -> dict:
    """
    Return polling intervals scaled by mult.
    mult < 1  = burst (high activity, faster captures)
    mult = 1  = normal
    mult > 1  = idle (slow down to save resources)
    mult = 10 = fully idle
    """
    base = cfg_module.LEVEL_SETTINGS.get(level, cfg_module.LEVEL_SETTINGS[3])
    custom = cfg_module.CUSTOM_INTERVAL
    app_poll  = custom if custom > 0 else max(1.0,  base["app_poll"]  * mult)
    clip_poll = custom if custom > 0 else max(1.0,  base["clip_poll"] * mult)
    fallback  = base["ocr_fallback"] or 60
    return {
        "app":          app_poll,
        "clip":         clip_poll,
        "terminal":     max(5.0,  10   * mult),
        "checkpoint":   max(60.0, 300  * mult),
        "ocr_trigger":  base["ocr_trigger"],
        "ocr_fallback": max(15.0, fallback * mult),
    }


# ── Main daemon ───────────────────────────────────────────────────────────

class WatcherDaemon:
    def __init__(self, store: Store, conf: cfg_module.Config):
        self.store = store
        self.cfg = conf
        self.level = conf.monitoring.level
        self.privacy_mode = conf.monitoring.privacy

        # Live state (tiny memory footprint)
        self._app_name = ""
        self._window_title = ""
        self._clip_hash = ""
        self._last_ocr_window = ""
        self._last_ocr_ts = 0.0
        self._last_checkpoint_ts = 0.0
        self._current_url = ""           # last known browser URL
        self._known_apps: set[str] = set()  # for detecting launches/quits
        self._vision_lock: asyncio.Lock | None = None  # set in run()
        self._last_vision_ts: float = 0.0             # rate-limit vision spawns from OCR
        self._screen_change_score: float = 0.0        # 0.0–1.0, how much screen changed last OCR
        self._prev_ocr_words: list[str] = []          # for change detection

        # Camera presence detection state (camera-specific, not in controller)
        self._presence_confirmed: bool = True         # assume present until camera says otherwise
        self._no_presence_frames: int = 0             # consecutive frames without a person

        self._stop_event = None          # set by run() so loops can trigger shutdown

        # OnionController: owns all adaptive scheduling decisions
        thresholds = list(getattr(conf.monitoring, "cpu_pressure_thresholds", [50, 70, 85]))
        self._ctrl = OnionController(thresholds=thresholds)

        # Input tracker (keyboard + mouse, pynput-based background threads)
        self._input_tracker: InputTracker | None = None

        self._file_watcher = files.FileWatcher()

    # ── Input tracker callbacks ────────────────────────────────────────────

    def _on_typed_text(self, text: str, app_name: str):
        """Called from pynput thread when a typing segment is flushed."""
        if _PAUSED:
            return
        self.store.log_event(
            channel="keyboard",
            summary=f"Typed ({len(text)} chars): {text[:60]}{'...' if len(text) > 60 else ''}",
            detail={"text": text, "app": app_name},
            category="content",
            importance=3,
            domain=app_name,
        )
        log.debug("Keyboard: typed segment saved (%d chars in %s)", len(text), app_name)

    def _on_mouse_action(self, action: str, zone: str):
        """Called from pynput thread on click or scroll."""
        if _PAUSED:
            return
        # Only log clicks with a zone (scrolls are noisy, skip)
        if action == "click" and zone and zone != "unknown":
            try:
                self.store.log_event(
                    channel="mouse",
                    summary=f"Click [{zone}] in {self._app_name}",
                    detail={"action": action, "zone": zone},
                    category="navigation",
                    importance=1,
                    domain=self._app_name,
                )
            except Exception:
                pass  # transient DB lock — mouse clicks are low-value, drop silently

    # ── Adaptive activity tracking ─────────────────────────────────────────

    def _bump_activity(self, amount: float):
        """Delegate to OnionController."""
        self._ctrl.bump_activity(amount)

    def _update_presence(self, score: float):
        """Delegate to OnionController."""
        self._ctrl.update_presence(_idle_seconds(), score)

    def _get_mult(self) -> float:
        """Return interval multiplier from OnionController (idle + activity + cpu pressure)."""
        return self._ctrl.get_mult(_idle_seconds(), self._presence_confirmed)

    async def run(self):
        global _PAUSED
        os.nice(10)   # low priority (Unix)
        self._vision_lock = asyncio.Lock()
        self._file_watcher.start()
        log.info("Onion Shell watcher started (level=%d, privacy=%s)",
                 self.level, self.privacy_mode)

        await self._init_snapshot()

        stop = asyncio.Event()
        self._stop_event = stop      # expose so _reinit_watch_loop can trigger shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        tasks = [
            asyncio.create_task(self._app_loop(), name="app"),
            asyncio.create_task(self._clip_loop(), name="clip"),
            asyncio.create_task(self._terminal_loop(), name="terminal"),
            asyncio.create_task(self._checkpoint_loop(), name="checkpoint"),
            asyncio.create_task(self._prune_loop(), name="prune"),
            asyncio.create_task(self._reinit_watch_loop(), name="reinit"),
            asyncio.create_task(self._cpu_adjust_loop(), name="cpu_adjust"),
            asyncio.create_task(self._browser_vision_loop(), name="browser_vision"),
            asyncio.create_task(self._system_change_loop(), name="system_changes"),
            asyncio.create_task(self._media_analysis_loop(), name="media_analysis"),
            # Phase 4: reader + bomber
            asyncio.create_task(self._reader_loop(), name="reader"),
            asyncio.create_task(self._reader_ping_loop(), name="reader_ping"),
            asyncio.create_task(self._bomber_loop(), name="bomber"),
        ]
        if self.level >= 3:
            tasks.append(asyncio.create_task(self._ocr_fallback_loop(), name="ocr_fallback"))
        if self.cfg.sensors.audio_enabled:
            tasks.append(asyncio.create_task(self._audio_loop(), name="audio"))
        if self.cfg.sensors.camera_enabled:
            tasks.append(asyncio.create_task(self._camera_loop(), name="camera"))

        # Start input tracker (keyboard + mouse) — runs in background threads.
        # Callbacks use call_soon_threadsafe to write to asyncio/SQLite safely.
        if input_available() and getattr(self.cfg.sensors, "input_enabled", True):
            _aio_loop = asyncio.get_running_loop()

            def _threadsafe_text(text: str, app: str):
                _aio_loop.call_soon_threadsafe(self._on_typed_text, text, app)

            def _threadsafe_mouse(action: str, zone: str):
                _aio_loop.call_soon_threadsafe(self._on_mouse_action, action, zone)

            self._input_tracker = InputTracker(
                on_text=_threadsafe_text,
                on_mouse=_threadsafe_mouse,
                get_app_name=lambda: self._app_name,
                get_privacy_mode=lambda: self.privacy_mode,
            )
            self._input_tracker.start()

        await stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._input_tracker:
            self._input_tracker.stop()
        self._file_watcher.stop()
        log.info("Watcher stopped.")

    # ── App loop ──────────────────────────────────────────────────────────

    async def _app_loop(self):
        while True:
            mult = self._get_mult()
            iv = _intervals(self.level, mult)
            await asyncio.sleep(iv["app"])

            if _PAUSED:
                continue

            state = await apps.get_app_state()
            if not state:
                continue

            app_name = state.get("app_name", "")
            window_title = state.get("window_title", "")
            open_apps_now = set(state.get("open_apps", []))

            # Track background app launches and quits (regardless of frontmost app)
            if self._known_apps and open_apps_now:
                launched = open_apps_now - self._known_apps
                quit_apps = self._known_apps - open_apps_now
                for a in sorted(launched):
                    self.store.log_event("system", f"App launched: {a}", {"app": a},
                                         category="system", importance=1, domain=a)
                    log.debug("App launched in background: %s", a)
                for a in sorted(quit_apps):
                    self.store.log_event("system", f"App quit: {a}", {"app": a},
                                         category="system", importance=1, domain=a)
                    log.debug("App quit: %s", a)
            if open_apps_now:
                self._known_apps = open_apps_now

            # Privacy: blocked app → pause all
            if privacy.is_blocked_app(app_name, self.privacy_mode):
                if self._app_name != app_name:
                    log.debug("Blocked app active: %s — capture paused", app_name)
                    self._app_name = app_name
                continue

            # App switch
            if app_name != self._app_name:
                summary = f"{self._app_name} → {app_name}" if self._app_name else app_name
                self.store.log_event("app", summary,
                                     {"from": self._app_name, "to": app_name},
                                     category="navigation", importance=2, domain=app_name)
                self._app_name = app_name
                self._bump_activity(0.4)   # switching apps = active work

                # OCR on app switch (level 2+)
                if self.level >= 2:
                    await self._do_ocr("app_switch")

            # Window title change
            if window_title != self._window_title:
                if window_title:
                    self.store.log_event("screen", f'Window: "{window_title}"',
                                         {"app": app_name},
                                         category="navigation", importance=1, domain=app_name)
                self._window_title = window_title
                self._bump_activity(0.2)   # new window = engaged

                # OCR on title change (level 3+)
                if self.level >= 3 and window_title:
                    await self._do_ocr("title_change")

            # Browser: poll URL every cycle (not just on title change)
            # Video pages often keep the same tab title while content changes
            app_cat = self._app_category(app_name)
            if app_cat == "browser" and self.level >= 3:
                url = await apps.get_browser_url(app_name)
                if url and url != self._current_url:
                    self._current_url = url
                    self._bump_activity(0.3)   # new page = browsing actively
                    domain = _extract_domain(url)
                    self.store.log_event("browser", f"URL: {url[:200]}", {"url": url},
                                         category="browsing", importance=3, domain=domain)
                    # Save checkpoint immediately so packager always has current URL
                    cmds = terminal.get_recent_commands(privacy=self.privacy_mode)
                    self.store.save_checkpoint(
                        app_name=self._app_name,
                        window_title=self._window_title,
                        clipboard_hash=self._clip_hash,
                        recent_commands=cmds[-10:],
                        url=url,
                    )
                    # Trigger delayed vision capture: video needs a few seconds to start
                    asyncio.create_task(
                        self._delayed_vision(delay=4, url=url),
                        name="vision_url_change"
                    )

            # File events (including folder creation/deletion)
            file_events = self._file_watcher.drain()
            if file_events and self.level >= 3:
                import os as _os
                new_dirs = [e for e in file_events
                            if e.get("kind") == "dir" and e.get("type") == "created"]
                by_dir: dict[str, int] = {}
                for ev in file_events:
                    d = _os.path.dirname(ev["path"])
                    by_dir[d] = by_dir.get(d, 0) + 1
                parts = []
                if new_dirs:
                    parts.append("New folders: " + ", ".join(
                        _os.path.basename(e["path"]) for e in new_dirs[:3]
                    ))
                parts.append(", ".join(
                    f"{n}× in .../{_os.path.basename(d)}" for d, n in list(by_dir.items())[:3]
                ))
                import os as _os2
                summary = "Files: " + " | ".join(p for p in parts if p)
                file_domain = _os2.path.basename(list(by_dir.keys())[0]) if by_dir else ""
                self.store.log_event("file", summary, {"events": file_events[:10]},
                                     category="files", importance=2, domain=file_domain)

    async def _init_snapshot(self):
        """Take a baseline snapshot of the machine's current state on startup."""
        log.info("Taking init snapshot...")
        try:
            # Current app + ALL running applications (baseline)
            state = await apps.get_app_state()
            if state:
                self._app_name = state.get("app_name", "")
                self._window_title = state.get("window_title", "")
                all_apps = state.get("open_apps", [])   # key is open_apps
                self._known_apps = set(all_apps)
            else:
                all_apps = []

            # Clipboard
            _, h = clipboard.get_clipboard(privacy=self.privacy_mode)
            if h:
                self._clip_hash = h

            # Recent terminal commands
            cmds = terminal.get_recent_commands(privacy=self.privacy_mode)

            # Save initial checkpoint (baseline state)
            self.store.save_checkpoint(
                app_name=self._app_name,
                window_title=self._window_title,
                clipboard_hash=self._clip_hash,
                recent_commands=cmds[-10:],
            )

            # Log full list of running apps as an init event
            if all_apps:
                self.store.log_event(
                    "init",
                    f"Running apps ({len(all_apps)}): " + ", ".join(all_apps[:20]),
                    {"all_apps": all_apps},
                    category="system", importance=0,
                )

            # Full current filesystem snapshot (ALL items, not just recent)
            fs_state = self._file_watcher.list_current()
            if fs_state:
                self.store.save_fs_state(fs_state)
                import os as _os
                summary_parts = []
                for d, info in fs_state.items():
                    label = _os.path.basename(d) or d
                    recent = info.get("recent", [])
                    recent_str = f" [*{', '.join(recent[:3])}]" if recent else ""
                    total = info.get("total", len(info.get("dirs", [])) + len(info.get("files", [])))
                    summary_parts.append(f"~/{label} ({total} items{recent_str})")
                self.store.log_event("init",
                    "Filesystem state: " + " | ".join(summary_parts),
                    {"watched_dirs": self._file_watcher._dirs},
                    category="system", importance=0,
                )

            # Initial OCR (level 3+)
            if self.level >= 3 and self._app_name:
                await self._do_ocr("init")

            # Initial vision description (level 3+, non-blocking)
            if self.level >= 3 and self.cfg.providers.ollama_vision_model:
                asyncio.create_task(self._do_vision(), name="vision_init")

            total_items = sum(
                info.get("total", len(info.get("dirs", [])) + len(info.get("files", [])))
                for info in fs_state.values()
            ) if fs_state else 0
            log.info("Init snapshot done (app=%s, running_apps=%d, total_items=%d)",
                     self._app_name, len(all_apps), total_items)
        except Exception as e:
            log.warning("Init snapshot failed: %s", e)

    def _app_category(self, app_name: str) -> str:
        for cat, names in cfg_module.APP_CATEGORIES.items():
            if app_name in names:
                return cat
        return "other"

    # ── Clipboard loop ────────────────────────────────────────────────────

    async def _clip_loop(self):
        while True:
            mult = self._get_mult()
            iv = _intervals(self.level, mult)
            await asyncio.sleep(iv["clip"])
            if _PAUSED or self.level < 2:
                continue
            if privacy.is_blocked_app(self._app_name, self.privacy_mode):
                continue

            text, h = clipboard.get_clipboard(privacy=self.privacy_mode)
            if h and h != self._clip_hash:
                self._clip_hash = h
                preview = text[:80].replace("\n", " ")
                self.store.log_event("clipboard",
                                     f"Clipboard changed ({len(text)} chars): {preview}",
                                     {"hash": h, "length": len(text)},
                                     category="content", importance=3)

    # ── Terminal loop ─────────────────────────────────────────────────────

    async def _terminal_loop(self):
        _prev: set[str] = set()
        while True:
            mult = self._get_mult()
            iv = _intervals(self.level, mult)
            await asyncio.sleep(iv["terminal"])
            if _PAUSED or self.level < 2:
                continue

            cmds = terminal.get_recent_commands(privacy=self.privacy_mode)
            new_cmds = [c for c in cmds if c not in _prev]
            if new_cmds:
                for cmd in new_cmds[-5:]:
                    self.store.log_event("terminal", f"$ {cmd}",
                                         category="coding", importance=4)
                _prev = set(cmds)

    # ── Checkpoint loop ───────────────────────────────────────────────────

    async def _checkpoint_loop(self):
        while True:
            mult = self._get_mult()
            iv = _intervals(self.level, mult)
            await asyncio.sleep(iv["checkpoint"])
            if _PAUSED:
                continue
            if privacy.is_blocked_app(self._app_name, self.privacy_mode):
                continue

            cmds = terminal.get_recent_commands(privacy=self.privacy_mode)
            self.store.save_checkpoint(
                app_name=self._app_name,
                window_title=self._window_title,
                clipboard_hash=self._clip_hash,
                recent_commands=cmds[-10:],
                url=self._current_url,
            )
            # Refresh full filesystem snapshot every checkpoint cycle
            fs_state = self._file_watcher.list_current()
            if fs_state:
                self.store.save_fs_state(fs_state)
            log.debug("Checkpoint saved (app=%s)", self._app_name)

    # ── OCR fallback loop (every N seconds regardless of title) ───────────

    async def _ocr_fallback_loop(self):
        """
        Periodically capture OCR even without a title change.
        Uses 10s micro-sleeps so the effective interval adapts immediately to
        activity changes (burst → ~24s, normal → 60s, idle → 600s at level 3).
        """
        if not cfg_module.LEVEL_SETTINGS.get(self.level, {}).get("ocr_fallback"):
            return
        while True:
            await asyncio.sleep(10)   # micro-sleep: check every 10s
            if _PAUSED or privacy.is_blocked_app(self._app_name, self.privacy_mode):
                continue
            iv = _intervals(self.level, self._get_mult())["ocr_fallback"]
            if time.time() - self._last_ocr_ts >= iv:
                await self._do_ocr("fallback")

    # ── Screen capture helper (OCR + optional vision) ─────────────────────

    async def _do_ocr(self, trigger: str):
        if privacy.is_blocked_app(self._app_name, self.privacy_mode):
            return

        # CPU critical pressure: skip OCR entirely
        if not self._ctrl.feature_enabled('ocr'):
            return

        # When user is absent, only do a "wake-up check" OCR every 10× normal interval.
        # This detects the user coming back (screen change) without wasting disk space.
        if not self._ctrl.user_present and trigger == "fallback":
            base_iv = _intervals(self.level, 1.0).get("ocr_fallback", 60)
            if time.time() - self._last_ocr_ts < base_iv * 10:
                return

        # Single capture: save JPEG (OCR moved to me2; runs at profile-update time)
        # Skip JPEG when user is absent to save disk space.
        screenshot_path_str = ""
        if self._ctrl.user_present:
            try:
                kd_path = self.cfg.monitoring.kaidata_path
                sp = kaidata_mod.screenshot_path(kd_path, self._app_name)
                saved = await screen.save_screenshot(sp)
                if saved:
                    screenshot_path_str = str(sp)
            except Exception as _e:
                log.debug("Screenshot save failed: %s", _e)

        if not screenshot_path_str and not self._ctrl.user_present:
            # Away and no screenshot — nothing to record
            return

        # screen_change_score: set to 1.0 (assume changed; me2 handles real analysis)
        self._screen_change_score = 1.0

        self.store.save_ocr(
            trigger=trigger,
            app_name=self._app_name,
            window_title=self._window_title,
            ocr_text="",
            confidence=0.0,
            screenshot_path=screenshot_path_str,
            screen_change_score=self._screen_change_score,
            is_away=0 if self._ctrl.user_present else 1,
        )
        self._last_ocr_ts = time.time()

        log.debug("Screenshot saved (trigger=%s, app=%s, path=%s, present=%s)",
                  trigger, self._app_name, screenshot_path_str or "none",
                  self._ctrl.user_present)

    async def _do_vision(self, url: str = "", trigger: str = "background", hq: bool = False):
        """Run vision LLM on current screen and store the description.

        hq=True  → best available model (llava:7b → Claude → moondream fallback).
                   Used for browser/video background loops and user ask time.
        hq=False → lightweight moondream only. Used for quick OCR-triggered checks.

        Uses a lock so concurrent calls skip rather than pile up.

        IMPORTANT: app_name and window_title are snapshotted at call time (before
        the inference starts), so the saved description is always paired with the
        correct window — not whatever the user switched to during the 15-27s inference.
        """
        if privacy.is_blocked_app(self._app_name, self.privacy_mode):
            log.info("Vision skipped — blocked app: %s", self._app_name)
            return
        if self._vision_lock is None:
            return
        if self._vision_lock.locked():
            log.info("Vision skipped — another capture in progress")
            return
        # Snapshot context NOW, before the slow inference begins
        snap_app   = self._app_name
        snap_title = self._window_title
        snap_url   = url or self._current_url
        async with self._vision_lock:
            try:
                if hq:
                    log.info("Vision HQ (trigger=%s) for app=%s url=%s",
                             trigger, snap_app, snap_url[:60])
                    desc = await screen.describe_screenshot_hq(
                        conf=self.cfg,
                        app_name=snap_app,
                        window_title=snap_title,
                        page_url=snap_url,
                    )
                else:
                    vision_model = self.cfg.providers.ollama_vision_model
                    log.info("Vision BG %s (trigger=%s) for app=%s",
                             vision_model, trigger, snap_app)
                    desc = await screen.describe_screenshot(
                        ollama_url=self.cfg.providers.ollama_url,
                        vision_model=vision_model,
                        app_name=snap_app,
                        window_title=snap_title,
                        page_url=snap_url,
                    )
                if desc:
                    # Check if window changed during inference — tag as potentially stale
                    window_changed = (self._window_title != snap_title or
                                      self._app_name != snap_app)
                    if window_changed and trigger == "background":
                        # Discard background captures where window changed during inference
                        # (they would describe the wrong content)
                        log.info(
                            "Vision discarded — window changed during inference "
                            "(was: %s | %s, now: %s | %s)",
                            snap_app, snap_title[:40], self._app_name, self._window_title[:40]
                        )
                        return
                    self.store.save_vision(
                        app_name=snap_app,          # use snapshot, not current self._app_name
                        window_title=snap_title,    # same — paired with the captured screenshot
                        description=desc,
                        trigger=trigger,
                    )
                    self._last_vision_ts = time.time()
                    log.info("Vision saved (trigger=%s, hq=%s, app=%s, len=%d): %s…",
                             trigger, hq, snap_app, len(desc), desc[:80])
                else:
                    log.info("Vision: empty description (app=%s, hq=%s)", self._app_name, hq)
            except Exception as e:
                import traceback
                log.warning("Vision failed: %s\n%s", e, traceback.format_exc())

    async def _delayed_vision(self, delay: float, url: str = ""):
        """Wait `delay` seconds then take an HQ vision snapshot (browser/video URL change)."""
        await asyncio.sleep(delay)
        if privacy.is_blocked_app(self._app_name, self.privacy_mode):
            return
        await self._do_vision(url=url, hq=True)

    # ── Browser vision loop (periodic capture while in browser) ──────────

    async def _browser_vision_loop(self):
        """
        When the active app is a browser or video player, take a vision snapshot
        periodically. Uses level-based vision_interval for auto mode, or custom interval.
        This ensures video content is captured even when the tab title/URL doesn't change.
        """
        while True:
            # Custom interval override wins; otherwise use level-based vision_interval
            custom = cfg_module.CUSTOM_INTERVAL
            if custom > 0:
                interval = custom
            else:
                lvl = cfg_module.LEVEL_SETTINGS.get(self.level, {})
                interval = lvl.get("vision_interval") or 30
                # Screen actively changing (video playing) → capture twice as often
                if self._screen_change_score > 0.3:
                    interval = max(5, interval // 2)
                # Idle → slow down 3×
                elif _idle_seconds() > 120:
                    interval = interval * 3
            await asyncio.sleep(interval)
            if _PAUSED or self.level < 3:
                continue
            if not self._ctrl.feature_enabled('vision'):
                continue    # CPU pressure ≥ heavy: skip vision
            if privacy.is_blocked_app(self._app_name, self.privacy_mode):
                continue
            if not self.cfg.providers.ollama_vision_model:
                continue
            app_cat = self._app_category(self._app_name)
            if app_cat in ("browser", "video"):
                # Use HQ model for browser/video — moondream is too weak to identify
                # video content, web articles, or on-screen objects accurately.
                await self._do_vision(hq=True)

    # ── CPU-based auto-adjust loop ────────────────────────────────────────

    async def _cpu_adjust_loop(self):
        """
        OnionController update loop — runs every 10s.
        Measures system + watcher CPU, feeds into OnionController (EMA, pressure tiers,
        activity decay). Writes enriched resource_status.json for the web UI.
        Also reads custom interval overrides from ~/.onion_shell/interval.
        Runs gc.collect() every 5 minutes.
        """
        import gc, json, os
        status_file = Path.home() / ".onion_shell" / "cpu_status"
        resource_file = Path.home() / ".onion_shell" / "resource_status.json"
        interval_file = Path.home() / ".onion_shell" / "interval"
        _gc_counter = 0
        pid = os.getpid()
        _ncpu = max(1, os.cpu_count() or 1)  # normalize multi-core CPU% to 0-100 scale
        while True:
            await asyncio.sleep(10)
            _gc_counter += 1

            # ── Measure CPU ───────────────────────────────────────────────────
            sys_cpu = 0.0
            proc_cpu = 0.0
            proc_rss_kb = 0
            try:
                # _cpu_percent() returns sum across all cores; normalise to 0-100
                sys_cpu = _cpu_percent() / _ncpu
                status_file.write_text(f"{sys_cpu:.1f}")
            except Exception:
                pass
            try:
                import subprocess as _sp
                ps_out = _sp.check_output(
                    ["ps", "-p", str(pid), "-o", "pid=,pcpu=,rss="],
                    stderr=_sp.DEVNULL, timeout=2
                ).decode().split()
                # ps pcpu is per-core %; normalize to 0-100 scale like sys_cpu
                proc_cpu = (float(ps_out[1]) if len(ps_out) > 1 else 0.0) / _ncpu
                proc_rss_kb = int(ps_out[2]) if len(ps_out) > 2 else 0
            except Exception:
                pass

            # ── Feed OnionController ──────────────────────────────────────────
            self._ctrl.update(sys_cpu=sys_cpu, watcher_cpu=proc_cpu)

            # ── Write enriched status for web UI ─────────────────────────────
            try:
                resource_file.write_text(json.dumps(
                    self._ctrl.to_dict(pid=pid, watcher_cpu=proc_cpu, rss_kb=proc_rss_kb)
                ))
            except Exception:
                pass

            # ── GC every ~5 min ───────────────────────────────────────────────
            if _gc_counter >= 30 or _idle_seconds() > 120:
                gc.collect()
                _gc_counter = 0

            # ── Custom interval override from web UI ──────────────────────────
            try:
                if interval_file.exists():
                    val = interval_file.read_text().strip()
                    if val and val != "auto":
                        cfg_module.CUSTOM_INTERVAL = max(2, int(val))
                    else:
                        cfg_module.CUSTOM_INTERVAL = 0
            except Exception:
                pass

    # ── System-level background change monitor ────────────────────────────

    async def _system_change_loop(self):
        """
        Detect file changes made by background agents/apps, not the user directly.
        Uses macOS `mdfind -onlyin` to find recently modified files across the whole
        home dir. Runs every 60s. Catches things like:
        - AI agent writing output files
        - Downloads completing
        - Cron jobs or services modifying config files
        """
        import platform as _plat
        if _plat.system() != "Darwin":
            return
        _prev_recent: set[str] = set()
        while True:
            await asyncio.sleep(60)
            if _PAUSED or self.level < 3:
                continue
            try:
                import subprocess
                # Find files modified in the last 90s, outside the user-visible watched dirs
                proc = await asyncio.create_subprocess_exec(
                    "mdfind",
                    "-onlyin", str(Path.home()),
                    "kMDItemFSContentChangeDate >= $time.now(-90)",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                recent_files = set(
                    p for p in stdout.decode().splitlines()
                    if p and not any(
                        skip in p for skip in (
                            "/.Trash/", "/.DS_Store", "/Cache", "/Caches",
                            "/.git/", "/node_modules/", "/__pycache__/",
                            "/onion_shell/db.sqlite",   # our own DB
                        )
                    )
                )
                new_files = recent_files - _prev_recent
                if new_files and _prev_recent:
                    # Group by parent dir, log compact summary
                    import os as _os
                    by_dir: dict[str, list[str]] = {}
                    for f in new_files:
                        d = _os.path.dirname(f).replace(str(Path.home()), "~")
                        by_dir.setdefault(d, []).append(_os.path.basename(f))
                    parts = [
                        f"{len(names)}× in {d}: {', '.join(names[:2])}"
                        + ("…" if len(names) > 2 else "")
                        for d, names in list(by_dir.items())[:5]
                    ]
                    summary = "Background changes: " + " | ".join(parts)
                    self.store.log_event("system", summary,
                                         {"files": list(new_files)[:20]},
                                         category="system", importance=1)
                    log.debug("System changes detected: %d files", len(new_files))
                _prev_recent = recent_files
            except Exception as e:
                log.debug("system_change_loop error: %s", e)

    # ── Flags watch loop (reinit + ask-trigger) ────────────────────────────

    async def _reinit_watch_loop(self):
        """Poll for flag files written by the web UI."""
        reinit_flag          = Path.home() / ".onion_shell" / "reinit"
        ask_flag             = Path.home() / ".onion_shell" / "ask_trigger"
        describe_media_flag  = Path.home() / ".onion_shell" / "describe_media_trigger"
        reboot_flag          = Path.home() / ".onion_shell" / "reboot_trigger"
        while True:
            await asyncio.sleep(2)

            if reboot_flag.exists():
                reboot_flag.unlink(missing_ok=True)
                log.info("Reboot flag detected — restarting daemon in 3 s")
                # Spawn a helper that waits 3s then relaunches the daemon.
                # The 3s gap lets the current process finish its clean shutdown first.
                onion_path = str(Path(__file__).parent.parent / "onion_shell.py")
                subprocess.Popen(
                    [sys.executable, "-c",
                     f"import time, subprocess; time.sleep(3); "
                     f"subprocess.Popen([{repr(sys.executable)}, {repr(onion_path)}, '_daemon'],"
                     f" stdout=open('/dev/null','w'), stderr=open('/dev/null','w'), start_new_session=True)"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                # Signal the asyncio run() loop to exit cleanly
                if self._stop_event is not None:
                    self._stop_event.set()
                return   # exit this coroutine

            if reinit_flag.exists():
                reinit_flag.unlink(missing_ok=True)
                log.info("Reinit flag detected — running init snapshot")
                await self._init_snapshot()

            if ask_flag.exists():
                ask_flag.unlink(missing_ok=True)
                log.info("Ask trigger — capturing fresh OCR + HQ vision for user question")
                await self._do_ocr("user_ask")
                # Reset rate-limit so vision fires immediately, use HQ model
                self._last_vision_ts = 0.0
                asyncio.create_task(
                    self._do_vision(trigger="user_ask", hq=True),
                    name="vision_ask_hq"
                )
                # Optional: capture what user just said
                if self.cfg.sensors.audio_enabled and audio_mod.is_available():
                    asyncio.create_task(self._capture_ask_audio(), name="audio_ask")

            if describe_media_flag.exists():
                try:
                    media_path_str = describe_media_flag.read_text().strip()
                    describe_media_flag.unlink(missing_ok=True)
                    if media_path_str:
                        media_path = Path(media_path_str)
                        log.info("Describe media trigger: %s", media_path_str)
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda p=media_path: media_analyzer.analyze_image_or_video(
                                p, self.cfg
                            )
                        )
                        if result:
                            self.store.save_file_description(str(media_path), **result)
                            log.info("On-demand media described: %s", media_path.name)
                except Exception as e:
                    log.warning("describe_media_trigger error: %s", e)

    # ── Audio ask capture (one-shot) ──────────────────────────────────────

    async def _capture_ask_audio(self):
        """Capture a brief audio segment when user triggers Ask."""
        result = await audio_mod.capture_segment(
            duration_s=8.0,
            language=self.cfg.sensors.audio_language,
            model_size=self.cfg.sensors.audio_model_size,
        )
        if result:
            self.store.save_audio(
                trigger="user_ask",
                app_name=self._app_name,
                transcript=result["transcript"],
                duration_s=result.get("duration_s", 0.0),
                language=result.get("language", ""),
                confidence=result.get("confidence", 0.0),
                category="communication",
            )
            log.info("Ask audio captured: %d chars", len(result["transcript"]))

    # ── Audio loop (continuous VAD-triggered transcription) ───────────────

    async def _audio_loop(self):
        """Continuously capture microphone speech segments and store transcripts."""
        if not audio_mod.is_available():
            log.info("Audio loop: mic/whisper not available — loop exits")
            return

        cap = audio_mod.AudioCapture(
            language=self.cfg.sensors.audio_language,
            model_size=self.cfg.sensors.audio_model_size,
        )
        log.info("Audio loop started")

        # Infer category from active app
        def _audio_category() -> str:
            meeting_apps = {"zoom", "teams", "discord", "google meet", "slack",
                            "facetime", "skype", "webex", "gotomeeting"}
            low = self._app_name.lower()
            if any(m in low for m in meeting_apps):
                return "communication"
            from config import APP_CATEGORIES
            if self._app_name in APP_CATEGORIES.get("code", set()):
                return "coding"
            return "communication"

        try:
            async for result in cap.segments():
                if _PAUSED or not self.cfg.sensors.audio_enabled:
                    cap.stop()
                    break
                if not self._ctrl.feature_enabled('audio'):
                    continue    # CPU pressure ≥ heavy: discard this audio segment
                if privacy.is_blocked_app(self._app_name, self.privacy_mode):
                    continue
                category = _audio_category()
                self.store.save_audio(
                    trigger="vad",
                    app_name=self._app_name,
                    transcript=result["transcript"],
                    duration_s=result.get("duration_s", 0.0),
                    language=result.get("language", ""),
                    confidence=result.get("confidence", 0.0),
                    category=category,
                )
                # Also log as an event so it shows up in timeline
                self.store.log_event(
                    channel="audio",
                    summary=f"Voice ({result.get('duration_s', 0):.0f}s): {result['transcript'][:80]}",
                    detail={"transcript": result["transcript"],
                            "duration_s": result.get("duration_s", 0.0),
                            "language": result.get("language", "")},
                    category=category,
                    importance=3,
                    domain=self._app_name,
                )
                log.info("Audio transcript saved: %d chars (%s)",
                         len(result["transcript"]), result.get("language", "?"))
        except Exception as e:
            log.warning("Audio loop error: %s", e)

    # ── Camera loop (periodic frame capture + presence detection) ─────────

    async def _camera_loop(self):
        """Periodically capture webcam frame, describe it, update presence state."""
        if not camera_mod.is_available():
            log.info("Camera loop: no camera available — loop exits")
            return

        log.info("Camera loop started (interval=%.0fs)", self.cfg.sensors.camera_interval)
        while True:
            await asyncio.sleep(self.cfg.sensors.camera_interval)
            if _PAUSED or not self.cfg.sensors.camera_enabled:
                continue
            if not self._ctrl.feature_enabled('camera'):
                continue    # CPU pressure ≥ light: skip camera
            if privacy.is_blocked_app(self._app_name, self.privacy_mode):
                continue

            try:
                desc = await camera_mod.describe_frame(
                    self.cfg, self._app_name, self._window_title
                )
                if desc:
                    self.store.save_vision(
                        self._app_name, self._window_title, desc,
                        trigger="background", source="camera"
                    )
                    # Update presence state
                    if camera_mod.has_person(desc):
                        self._presence_confirmed = True
                        self._no_presence_frames = 0
                        log.debug("Camera: person detected")
                    else:
                        self._no_presence_frames += 1
                        if self._no_presence_frames >= 3:
                            self._presence_confirmed = False
                            log.info("Camera: no person for %d frames — marking idle",
                                     self._no_presence_frames)
                else:
                    # No description (static scene / motion below threshold)
                    # Still count as a presence frame gap if camera is responding
                    pass
            except Exception as e:
                log.warning("Camera loop error: %s", e)

    # ── Media analysis loop ───────────────────────────────────────────────

    async def _media_analysis_loop(self):
        """
        Periodically scan Desktop/Downloads/Documents/Pictures for unanalyzed
        media files and store descriptions in the file_descriptions table.
        Runs every 5 minutes regardless of sensor config.
        """
        SCAN_DIRS = [Path.home() / d
                     for d in ("Desktop", "Downloads", "Documents", "Pictures")]
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            if _PAUSED:
                continue
            if not self._ctrl.feature_enabled('media'):
                continue    # CPU pressure ≥ heavy: skip media analysis
            for d in SCAN_DIRS:
                if not d.exists():
                    continue
                try:
                    for f in d.iterdir():
                        if not f.is_file():
                            continue
                        ftype = media_analyzer.is_media_file(f)
                        if not ftype:
                            continue
                        try:
                            existing = self.store.get_file_description(str(f))
                            if existing and (time.time() - existing["ts"]) < 3600:
                                continue  # analyzed within last hour, skip
                            result = await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda p=f: media_analyzer.analyze_image_or_video(p, self.cfg)
                            )
                            if result:
                                self.store.save_file_description(str(f), **result)
                                log.debug("Media analyzed: %s (%s)", f.name, result["file_type"])
                        except Exception as e:
                            log.debug("Media analysis error for %s: %s", f.name, e)
                except PermissionError:
                    pass
                except Exception as e:
                    log.debug("Media scan error in %s: %s", d, e)

    # ── Reader loop ───────────────────────────────────────────────────────

    async def _reader_loop(self):
        """Scan local directories every 5 minutes, update reader_refs."""
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            if _PAUSED:
                continue
            if not self._ctrl.feature_enabled('reader'):
                continue    # CPU pressure ≥ heavy: skip file scanning
            try:
                updated = await reader_mod.scan_directories(self.store, self.cfg)
                if updated:
                    log.debug("Reader: %d refs updated", updated)
            except Exception as e:
                log.debug("Reader scan error: %s", e)

    async def _reader_ping_loop(self):
        """Ping all reader refs every 30 minutes to detect deleted files."""
        await asyncio.sleep(60)  # initial delay — let reader_loop populate first
        while True:
            try:
                alive, dead = reader_mod.ping_refs(self.store)
                if dead:
                    log.info("Reader ping: %d alive, %d dead (cleaned up)", alive, dead)
            except Exception as e:
                log.debug("Reader ping error: %s", e)
            await asyncio.sleep(1800)  # every 30 minutes

    # ── Bomber loop ───────────────────────────────────────────────────────

    async def _bomber_loop(self):
        """Bomber driven by bomb_score (ratio + redundancy + time pressure).
        Also triggers immediately if storage exceeds budget; loops until within budget.
        Reads storage_gb from config each cycle so web UI changes take effect immediately.
        """
        from core.bomber import (
            run_bomber, should_run_bomber, _data_similarity,
        )
        from core.kaidata import load_bomber_state, save_bomber_state

        kd_path = self.cfg.monitoring.kaidata_path
        kd_dir = kaidata_mod.get_kaidata_dir(kd_path)
        state = load_bomber_state(kd_path)
        last_run = state.get("last_run", 0.0)

        while True:
            await asyncio.sleep(600)  # check every 10 minutes
            try:
                live_cfg = cfg_module.load()
                storage_gb = live_cfg.monitoring.storage_gb
            except Exception:
                storage_gb = getattr(self.cfg.monitoring, "storage_gb", 2.0)
            max_bytes = int(storage_gb * 1024 ** 3)

            try:
                from core.kaidata_reader import KaiDataReader
                reader = KaiDataReader(str(kaidata_mod.get_db_path(kd_path)))
                cr = reader.change_ratio(last_run)
                ratio = cr["combined"]["ratio"]
                similarity = _data_similarity(self.store, last_run)
            except Exception as e:
                log.warning("Bomber: could not compute bomb_score signals: %s", e)
                continue

            should_run, bomb_score = should_run_bomber(
                ratio, similarity, last_run, max_bytes, kd_dir
            )
            if not should_run:
                continue

            # Storage-exceeded mode: loop until within budget
            loop_limit = 10
            for _ in range(loop_limit):
                try:
                    summary = run_bomber(self.store, kd_dir, max_bytes,
                                         bomb_score=bomb_score)
                    last_run = time.time()
                    save_bomber_state({"last_run": last_run}, kd_path)
                    if summary.get("rows_deleted"):
                        log.info("Bomber (score=%.2f): %s",
                                 bomb_score, summary["rows_deleted"])
                except Exception as e:
                    log.warning("Bomber error: %s", e)
                    break

                # Check if we've brought storage within budget
                from core.kaidata import total_disk_usage
                if total_disk_usage(str(kd_dir)) <= max_bytes:
                    break
                bomb_score = 1.0  # keep pressure at max for subsequent passes

    # ── Prune loop ────────────────────────────────────────────────────────

    async def _prune_loop(self):
        while True:
            await asyncio.sleep(3600)
            # Time-based prune (short-term cleanup, superseded by bomber for long-term)
            ret = self.cfg.retention
            self.store.prune(
                events_hours=ret.events_hours,
                checkpoints_hours=ret.checkpoints_hours,
                ocr_hours=ret.ocr_hours,
            )
            # Prune stale file descriptions and dead reader refs
            self.store.prune_file_descriptions()
            self.store.delete_dead_refs(older_than_hours=24.0)
            log.debug("DB pruned")


def run_daemon():
    import os as _os
    # Write PID so the web UI and reboot logic always know the current process
    pid_file = Path.home() / ".onion_shell" / "watcher.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(_os.getpid()))

    conf = cfg_module.load()
    # Initialise kaiData directory and use its DB
    kd_path = conf.monitoring.kaidata_path
    kaidata_mod.init_kaidata(kd_path)
    db_path = kaidata_mod.get_db_path(kd_path)
    store = Store(db_path)
    daemon = WatcherDaemon(store, conf)
    asyncio.run(daemon.run())
