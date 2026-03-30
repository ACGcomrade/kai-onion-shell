"""
AppWatcher — active application + window title.
macOS: osascript. Linux: xdotool. Windows: ctypes win32.
"""
from __future__ import annotations
import asyncio
import logging
import platform
import subprocess

log = logging.getLogger("onion.apps")
_OS = platform.system()   # "Darwin" | "Linux" | "Windows"

_MACOS_SCRIPT = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set n to name of frontApp
    set t to ""
    try
        set t to name of first window of frontApp
    end try
    set allApps to name of every application process whose background only is false
    set AppleScript's text item delimiters to "~~"
    set appsStr to allApps as string
    set AppleScript's text item delimiters to ""
    return n & "|" & t & "|" & appsStr
end tell
"""

# Browser-specific URL scripts (macOS)
_SAFARI_URL = 'tell application "Safari" to get URL of current tab of front window'
_CHROME_URL = 'tell application "Google Chrome" to get URL of active tab of first window'
_ARC_URL    = 'tell application "Arc" to get URL of active tab of front window'


async def get_app_state() -> dict | None:
    if _OS == "Darwin":
        return await _macos_app()
    elif _OS == "Linux":
        return await _linux_app()
    elif _OS == "Windows":
        return _windows_app()
    return None


async def get_browser_url(app_name: str) -> str:
    """Try to get current browser URL (macOS only). Returns '' on failure."""
    if _OS != "Darwin":
        return ""
    script = None
    if "Safari" in app_name:
        script = _SAFARI_URL
    elif "Chrome" in app_name:
        script = _CHROME_URL
    elif "Arc" in app_name:
        script = _ARC_URL
    if not script:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        return stdout.decode().strip()
    except Exception:
        return ""


async def _macos_app() -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", _MACOS_SCRIPT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            return None
        parts = stdout.decode("utf-8", errors="replace").strip().split("|", 2)
        if len(parts) < 3:
            return None
        open_apps = [a.strip() for a in parts[2].split("~~") if a.strip()]
        return {"app_name": parts[0].strip(), "window_title": parts[1].strip(),
                "open_apps": open_apps}
    except Exception as e:
        log.debug("macOS app poll error: %s", e)
        return None


async def _linux_app() -> dict | None:
    """Use xdotool if available."""
    try:
        p1 = await asyncio.create_subprocess_exec(
            "xdotool", "getactivewindow", "getwindowname",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(p1.communicate(), timeout=3.0)
        title = out.decode().strip()
        return {"app_name": "", "window_title": title, "open_apps": []}
    except Exception:
        return None


def _windows_app() -> dict | None:
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        return {"app_name": "", "window_title": title, "open_apps": []}
    except Exception:
        return None
