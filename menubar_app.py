#!/usr/bin/env python3
"""
Onion Shell — macOS Menu Bar App

Lives in the menu bar with a 🧅 icon.
Runs the watcher daemon in a background thread.
Click to see status, pause/resume, adjust level.
"""
from __future__ import annotations
import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import rumps

# ── Bootstrap: start watcher thread when app launches ─────────────────────

_watcher_thread: threading.Thread | None = None
_watcher_running = False


def _run_watcher_thread():
    global _watcher_running
    _watcher_running = True
    try:
        import config as cfg_module
        from config import DB_PATH
        from core.store import Store
        from watcher.daemon import WatcherDaemon
        conf = cfg_module.load()
        store = Store(DB_PATH)
        daemon = WatcherDaemon(store, conf)
        asyncio.run(daemon.run())
    except Exception as e:
        print(f"[watcher] error: {e}")
    finally:
        _watcher_running = False


def start_watcher():
    global _watcher_thread
    if _watcher_thread and _watcher_thread.is_alive():
        return
    _watcher_thread = threading.Thread(target=_run_watcher_thread, daemon=True, name="onion-watcher")
    _watcher_thread.start()


# ── Main App ───────────────────────────────────────────────────────────────

class OnionShellApp(rumps.App):
    def __init__(self):
        super().__init__(
            name="Onion Shell",
            title="🧅",
            quit_button=None,
        )
        self._paused = False
        self._setup_menu()
        start_watcher()
        # Status updater
        self._timer = rumps.Timer(self._update_status, 10)
        self._timer.start()

    def _setup_menu(self):
        self.menu = [
            rumps.MenuItem("Status", callback=self._show_status),
            rumps.MenuItem("Recent Activity", callback=self._show_history),
            None,
            rumps.MenuItem("⏸ Pause Capture", callback=self._toggle_pause),
            rumps.MenuItem("Monitoring Level", callback=None),
            self._level_menu(),
            None,
            rumps.MenuItem("Quit Onion Shell", callback=rumps.quit_application),
        ]

    def _level_menu(self):
        menu = rumps.MenuItem("Monitoring Level")
        for lvl, label in [
            (1, "1 — Minimal (app only)"),
            (2, "2 — Basic"),
            (3, "3 — Standard ✓"),
            (4, "4 — Detailed"),
            (5, "5 — Maximum"),
        ]:
            item = rumps.MenuItem(label, callback=lambda _, l=lvl: self._set_level(l))
            menu.add(item)
        return menu

    def _update_status(self, _):
        """Update menu bar icon based on state."""
        if self._paused:
            self.title = "🧅⏸"
        elif _watcher_running:
            self.title = "🧅"
        else:
            self.title = "🧅⚠"

    # ── Menu callbacks ────────────────────────────────────────────────────

    @rumps.clicked("Status")
    def _show_status(self, _):
        try:
            import config as cfg_module
            from config import DB_PATH
            from core.store import Store
            conf = cfg_module.load()
            store = Store(DB_PATH)
            cp = store.latest_checkpoint()
            size_kb = DB_PATH.stat().st_size // 1024 if DB_PATH.exists() else 0
            ocr = store.latest_ocr(max_age_minutes=30)

            lines = [
                f"Level: {conf.monitoring.level}  Privacy: {conf.monitoring.privacy}",
                f"Events: {store.event_count()}  DB: {size_kb}KB",
                f"Watcher: {'running ✓' if _watcher_running else 'stopped ✗'}",
            ]
            if cp:
                from datetime import datetime
                ts = datetime.fromtimestamp(cp["ts"]).strftime("%H:%M:%S")
                lines.append(f"Last checkpoint: {ts}")
                lines.append(f"App: {cp['app_name']} — {cp['window_title'][:40]}")
            if ocr:
                from datetime import datetime
                ts = datetime.fromtimestamp(ocr["ts"]).strftime("%H:%M:%S")
                lines.append(f"Last OCR: {ts} ({len(ocr['ocr_text'].split())} words)")

            rumps.alert(title="🧅 Onion Shell Status", message="\n".join(lines))
        except Exception as e:
            rumps.alert(title="Error", message=str(e))

    @rumps.clicked("Recent Activity")
    def _show_history(self, _):
        try:
            from config import DB_PATH
            from core.store import Store
            from datetime import datetime
            store = Store(DB_PATH)
            events = store.recent_events(minutes=30, limit=20)
            if not events:
                rumps.alert("No activity in the last 30 minutes.")
                return
            lines = [f"Last 30 minutes ({len(events)} events):\n"]
            for ev in events[-15:]:
                ts = datetime.fromtimestamp(ev["ts"]).strftime("%H:%M")
                lines.append(f"{ts} [{ev['channel'][:4]}] {ev['summary'][:60]}")
            rumps.alert(title="🧅 Recent Activity", message="\n".join(lines))
        except Exception as e:
            rumps.alert(title="Error", message=str(e))

    @rumps.clicked("⏸ Pause Capture")
    def _toggle_pause(self, sender):
        self._paused = not self._paused
        pause_file = Path.home() / ".onion_shell" / "paused"
        if self._paused:
            pause_file.touch()
            sender.title = "▶ Resume Capture"
            self.title = "🧅⏸"
        else:
            pause_file.unlink(missing_ok=True)
            sender.title = "⏸ Pause Capture"
            self.title = "🧅"

    def _set_level(self, level: int):
        import subprocess
        subprocess.run([
            sys.executable,
            str(ROOT / "onion_shell.py"),
            "config", "set", "level", str(level),
        ], capture_output=True)
        rumps.alert(f"Monitoring level set to {level}.\nRestart app to apply.")

    @rumps.clicked("Quit Onion Shell")
    def _quit(self, _):
        rumps.quit_application()


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OnionShellApp().run()
