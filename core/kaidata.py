"""
core/kaidata.py — kaiData directory management.

All persistent data lives in kaiData/:
  kai.db           — SQLite database (events, OCR, vision, reader refs, etc.)
  screenshots/     — Raw JPEG screenshots from watcher (YYYY/MM/DD/HH-MM-SS_{app}.jpg)
  .bomber_state.json — Bomber last-run state

Config (~/.onion_shell/config.toml) and runtime files stay in ~/.onion_shell/.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from pathlib import Path


def _resolve_kaidata_dir(kaidata_path: str = "") -> Path:
    """Return the kaiData directory path, auto-detecting if not configured."""
    if kaidata_path:
        return Path(kaidata_path).expanduser().resolve()
    # Auto: next to the project directory (kai onion shell's parent = kaiMemoriesProject)
    project_root = Path(__file__).parent.parent   # .../kai onion shell/
    return project_root.parent / "kaiData"        # .../kaiMemoriesProject/kaiData/


def get_kaidata_dir(kaidata_path: str = "") -> Path:
    return _resolve_kaidata_dir(kaidata_path)


def get_db_path(kaidata_path: str = "") -> Path:
    return _resolve_kaidata_dir(kaidata_path) / "kai.db"


def get_screenshots_dir(kaidata_path: str = "") -> Path:
    return _resolve_kaidata_dir(kaidata_path) / "screenshots"


def get_bomber_state_path(kaidata_path: str = "") -> Path:
    return _resolve_kaidata_dir(kaidata_path) / ".bomber_state.json"


def init_kaidata(kaidata_path: str = "") -> Path:
    """
    Create kaiData directory structure if it doesn't exist.
    Returns the kaiData Path.
    """
    kd = _resolve_kaidata_dir(kaidata_path)
    kd.mkdir(parents=True, exist_ok=True)
    (kd / "screenshots").mkdir(exist_ok=True)
    return kd


def screenshot_path(kaidata_path: str, app_name: str, ts: float | None = None) -> Path:
    """
    Generate a screenshot file path under kaiData/screenshots/YYYY/MM/DD/HH-MM-SS_{app}.jpg
    Creates parent directories on demand.
    """
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    day_dir = get_screenshots_dir(kaidata_path) / dt.strftime("%Y") / dt.strftime("%m") / dt.strftime("%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    safe_app = "".join(c if c.isalnum() or c in "-_" else "_" for c in app_name)[:20]
    filename = dt.strftime("%H-%M-%S") + f"_{safe_app}.jpg"
    return day_dir / filename


def screenshots_disk_usage(kaidata_path: str = "") -> int:
    """Return total bytes used by all screenshot files."""
    screens_dir = get_screenshots_dir(kaidata_path)
    if not screens_dir.exists():
        return 0
    return sum(f.stat().st_size for f in screens_dir.rglob("*.jpg") if f.is_file())


def total_disk_usage(kaidata_path: str = "") -> int:
    """Return total bytes used by kaiData (db + screenshots)."""
    kd = _resolve_kaidata_dir(kaidata_path)
    if not kd.exists():
        return 0
    return sum(f.stat().st_size for f in kd.rglob("*") if f.is_file())


# ── Bomber state ────────────────────────────────────────────────────────────

def load_bomber_state(kaidata_path: str = "") -> dict:
    p = get_bomber_state_path(kaidata_path)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {"last_run": 0}


def save_bomber_state(state: dict, kaidata_path: str = "") -> None:
    p = get_bomber_state_path(kaidata_path)
    try:
        p.write_text(json.dumps(state))
    except Exception:
        pass
