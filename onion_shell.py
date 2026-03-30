#!/usr/bin/env python3
"""
Onion Shell — CLI entry point.

Usage:
  python3 onion_shell.py init            # initialise kaiData directory (first run)
  python3 onion_shell.py start           # start background watcher
  python3 onion_shell.py history         # show recent activity
  python3 onion_shell.py status          # show current state + stats
  python3 onion_shell.py pause           # pause capture
  python3 onion_shell.py resume          # resume capture
  python3 onion_shell.py config set storage_gb 10
  python3 onion_shell.py config set privacy strict
  python3 onion_shell.py install-service    # install macOS LaunchAgent (auto-start + no App Nap)
  python3 onion_shell.py uninstall-service  # remove LaunchAgent
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Make sure project root is on path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import config as cfg_module
from core.store import Store
from core.kaidata import get_db_path, init_kaidata
from core.packager import build_context, estimate_tokens


def _store() -> Store:
    conf = cfg_module.load()
    kd_path = conf.monitoring.kaidata_path
    init_kaidata(kd_path)
    return Store(get_db_path(kd_path))


# ── history ────────────────────────────────────────────────────────────────

def cmd_history(minutes: float = 60.0):
    store = _store()
    events = store.recent_events(minutes=minutes)
    if not events:
        print(f"No events in the last {int(minutes)} minutes.")
        return
    print(f"Last {int(minutes)} minutes ({len(events)} events):\n")
    for ev in events:
        ts = datetime.fromtimestamp(ev["ts"]).strftime("%H:%M:%S")
        print(f"  {ts}  [{ev['channel']:8s}] {ev['summary']}")


# ── status ─────────────────────────────────────────────────────────────────

def cmd_status(show_context: bool = False):
    store = _store()
    conf = cfg_module.load()
    cp = store.latest_checkpoint()

    db_path = get_db_path(conf.monitoring.kaidata_path)
    print("=== Onion Shell Status ===")
    print(f"Level:    {conf.monitoring.level}  Privacy: {conf.monitoring.privacy}")
    print(f"Database: {db_path}")
    size_kb = db_path.stat().st_size // 1024 if db_path.exists() else 0
    print(f"DB size:  {size_kb} KB")
    print(f"Events:   {store.event_count()}")

    if cp:
        ts = datetime.fromtimestamp(cp["ts"]).strftime("%H:%M:%S")
        print(f"\nLast checkpoint ({ts}):")
        print(f"  App:     {cp['app_name']}")
        print(f"  Window:  {cp['window_title']}")
        if cp["recent_commands"]:
            print(f"  Commands: {', '.join(cp['recent_commands'][-3:])}")

    latest_ocr = store.latest_ocr(max_age_minutes=30)
    if latest_ocr:
        ts = datetime.fromtimestamp(latest_ocr["ts"]).strftime("%H:%M:%S")
        word_count = len(latest_ocr["ocr_text"].split())
        print(f"\nLatest OCR ({ts}): {word_count} words, conf={latest_ocr['confidence']:.2f}")

    if show_context:
        print("\n─── Sample context (10min window) ───")
        ctx = build_context("(sample)", store, window_minutes=10,
                            level=conf.monitoring.level)
        print(ctx)
        print(f"─── ({estimate_tokens(ctx)} tokens est) ───")


# ── init ───────────────────────────────────────────────────────────────────

def cmd_init():
    """Initialise kaiData directory and database."""
    conf = cfg_module.load()
    kd_path = conf.monitoring.kaidata_path
    from core.kaidata import init_kaidata, get_kaidata_dir, get_db_path
    kd = init_kaidata(kd_path)
    db = get_db_path(kd_path)

    # Create DB + all tables
    store = Store(db)
    print(f"✓ kaiData directory: {kd}")
    print(f"✓ Database: {db}")
    print(f"✓ Screenshots: {kd / 'screenshots'}")

    # Migrate from legacy ~/.onion_shell/db.sqlite if it exists and kaiData DB is empty
    legacy_db = cfg_module.DATA_DIR / "db.sqlite"
    if legacy_db.exists() and legacy_db != db:
        try:
            import sqlite3
            src = sqlite3.connect(str(legacy_db))
            dst = sqlite3.connect(str(db))
            for line in src.iterdump():
                # Skip CREATE TABLE/INDEX — already created by Store DDL
                if line.startswith("CREATE TABLE") or line.startswith("CREATE INDEX") or \
                   line.startswith("CREATE UNIQUE"):
                    continue
                try:
                    dst.execute(line)
                except Exception:
                    pass
            dst.commit()
            src.close()
            dst.close()
            print(f"✓ Migrated data from {legacy_db}")
        except Exception as e:
            print(f"  (Migration skipped: {e})")

    print(f"\nStorage budget: {conf.monitoring.storage_gb} GB")
    print("Run 'python3 onion_shell.py start' to begin capturing.")


# ── config ─────────────────────────────────────────────────────────────────

def cmd_config(args: list[str]):
    cfg_module.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not cfg_module.CONFIG_FILE.exists():
        cfg_module.CONFIG_FILE.write_text(cfg_module.DEFAULT_CONFIG)

    if len(args) < 3 or args[0] != "set":
        print("Usage: onion config set <key> <value>")
        print("  Keys: level (1-5), privacy (strict|standard|minimal)")
        print(f"  Config file: {cfg_module.CONFIG_FILE}")
        return

    key, value = args[1], args[2]
    text = cfg_module.CONFIG_FILE.read_text()

    if key == "level":
        import re
        text = re.sub(r"(level\s*=\s*)\d+", f"\\g<1>{value}", text)
        cfg_module.CONFIG_FILE.write_text(text)
        print(f"Monitoring level set to {value}")
    elif key == "privacy":
        import re
        text = re.sub(r'(privacy\s*=\s*")[^"]*"', f'\\g<1>{value}"', text)
        cfg_module.CONFIG_FILE.write_text(text)
        print(f"Privacy mode set to {value}")
    elif key == "model":
        import re
        text = re.sub(r'(ollama_model\s*=\s*")[^"]*"', f'\\g<1>{value}"', text)
        cfg_module.CONFIG_FILE.write_text(text)
        print(f"Ollama model set to {value}")
    elif key == "storage_gb":
        import re
        if re.search(r"storage_gb\s*=", text):
            text = re.sub(r"(storage_gb\s*=\s*)[\d.]+[^\n]*", f"\\g<1>{value}", text)
        else:
            text = re.sub(r"(\[monitoring\][^\n]*\n)", f"\\g<1>storage_gb = {value}\n", text, count=1)
        cfg_module.CONFIG_FILE.write_text(text)
        print(f"Storage budget set to {value} GB")
    else:
        print(f"Unknown key: {key}. Supported: level, privacy, model, storage_gb")


# ── service install / uninstall ────────────────────────────────────────────

LABEL = "com.kaimemories.onionshell"
PLIST_TEMPLATE = ROOT / "launchd" / "com.kaimemories.onionshell.plist"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
INSTALLED_PLIST = LAUNCH_AGENTS / f"{LABEL}.plist"


def cmd_install_service():
    """Install a macOS LaunchAgent so the watcher auto-starts at login and never gets
    killed by App Nap. Stops any running watcher first, then loads the new agent."""
    import shutil, subprocess as sp

    if not PLIST_TEMPLATE.exists():
        print(f"Error: plist template not found at {PLIST_TEMPLATE}")
        return

    # Fill in real paths
    python3 = sys.executable
    script = str(ROOT / "onion_shell.py")
    home = str(Path.home())
    text = PLIST_TEMPLATE.read_text()
    text = text.replace("__PYTHON__", python3)
    text = text.replace("__SCRIPT__", script)
    text = text.replace("__HOME__", home)

    # Stop existing watcher if running
    pid_file = cfg_module.DATA_DIR / "watcher.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            pid_file.unlink(missing_ok=True)
            print(f"Stopped existing watcher (PID {pid})")
        except Exception:
            pass

    # Unload previous version if already installed
    if INSTALLED_PLIST.exists():
        try:
            sp.run(["launchctl", "unload", "-w", str(INSTALLED_PLIST)],
                   check=False, capture_output=True)
        except Exception:
            pass

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    INSTALLED_PLIST.write_text(text)
    print(f"✓ Installed: {INSTALLED_PLIST}")

    result = sp.run(["launchctl", "load", "-w", str(INSTALLED_PLIST)],
                    capture_output=True, text=True)
    if result.returncode == 0:
        print(f"✓ Service loaded — watcher will auto-start at login and restart on crash")
        print(f"  Logs: {home}/.onion_shell/watcher.log")
        print(f"  Stop: python3 onion_shell.py uninstall-service")
    else:
        print(f"launchctl load failed: {result.stderr.strip() or result.stdout.strip()}")


def cmd_uninstall_service():
    """Remove the LaunchAgent and stop the watcher."""
    import subprocess as sp

    if not INSTALLED_PLIST.exists():
        print("LaunchAgent not installed.")
        return

    result = sp.run(["launchctl", "unload", "-w", str(INSTALLED_PLIST)],
                    capture_output=True, text=True)
    INSTALLED_PLIST.unlink(missing_ok=True)
    if result.returncode == 0 or not result.stderr.strip():
        print(f"✓ Service unloaded and removed")
    else:
        print(f"launchctl unload: {result.stderr.strip()}")
        print("Plist removed; you may need to restart for full cleanup.")


# ── start (watcher daemon) ─────────────────────────────────────────────────

def cmd_start(foreground: bool = False):
    if foreground:
        print("Starting watcher in foreground (Ctrl+C to stop)…")
        from watcher.daemon import run_daemon
        run_daemon()
    else:
        print("Starting watcher in background…")
        pid_file = cfg_module.DATA_DIR / "watcher.pid"
        proc = subprocess.Popen(
            [sys.executable, __file__, "_daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        print(f"Watcher started (PID {proc.pid})")
        print("Stop with: python3 onion_shell.py stop")


def cmd_stop():
    pid_file = cfg_module.DATA_DIR / "watcher.pid"
    if not pid_file.exists():
        print("No watcher PID file found.")
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink()
        print(f"Watcher (PID {pid}) stopped.")
    except ProcessLookupError:
        print(f"Process {pid} not found (may have already stopped).")
        pid_file.unlink(missing_ok=True)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "_daemon":
        from watcher.daemon import run_daemon
        run_daemon()

    elif cmd == "init":
        cmd_init()

    elif cmd == "start":
        fg = "--fg" in args or "--foreground" in args
        cmd_start(foreground=fg)

    elif cmd == "stop":
        cmd_stop()

    elif cmd == "history":
        minutes = 60.0
        for i, a in enumerate(args[1:]):
            try:
                minutes = float(a)
            except ValueError:
                pass
        cmd_history(minutes=minutes)

    elif cmd == "status":
        cmd_status(show_context="--show-context" in args)

    elif cmd == "pause":
        # Write pause flag file
        (cfg_module.DATA_DIR / "paused").touch()
        print("Capture paused. Resume with: python3 onion_shell.py resume")

    elif cmd == "resume":
        pause_file = cfg_module.DATA_DIR / "paused"
        if pause_file.exists():
            pause_file.unlink()
        print("Capture resumed.")

    elif cmd == "config":
        cmd_config(args[1:])

    elif cmd == "install-service":
        cmd_install_service()

    elif cmd == "uninstall-service":
        cmd_uninstall_service()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
