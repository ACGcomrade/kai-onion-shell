"""
FileWatcher — event-driven file monitoring via watchdog.
Cross-platform (FSEvents/inotify/ReadDirectoryChangesW).
"""
from __future__ import annotations
import logging
import os
import queue
import time
from pathlib import Path

log = logging.getLogger("onion.files")

_IGNORE = {".DS_Store", ".pyc", "__pycache__", ".swp", ".tmp", ".log"}

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG = True
except ImportError:
    _WATCHDOG = False
    log.warning("watchdog not installed: pip install watchdog")

_DEFAULT_DIRS = [
    Path.home() / "Documents",
    Path.home() / "Desktop",
    Path.home() / "Downloads",
    Path.home() / "Projects",
    Path.home() / "Developer",
    # iCloud Drive (synced files changed by cloud/agents)
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
]

# Additional dirs to watch for background agent/system activity
# (watched but NOT included in the user-facing filesystem snapshot)
_SYSTEM_WATCH_DIRS = [
    Path.home() / "Library" / "Logs",
]


class FileWatcher:
    def __init__(self, watch_dirs: list[str] | None = None):
        env = os.environ.get("ONION_WATCH_DIRS", "")
        if watch_dirs:
            self._dirs = watch_dirs
        elif env:
            self._dirs = [d.strip() for d in env.split(",") if d.strip()]
        else:
            self._dirs = [str(d) for d in _DEFAULT_DIRS if d.exists()]
        self._q: queue.Queue = queue.Queue()
        self._observer = None

    def scan_initial_state(self, hours: float = 24.0) -> list[dict]:
        """Scan watched dirs for items created/modified in the last N hours.
        Returns a compact list for the init snapshot."""
        cutoff = time.time() - hours * 3600
        results = []
        for d in self._dirs:
            root = Path(d)
            if not root.exists():
                continue
            try:
                # Top-level items + one level deep — don't recurse fully
                for item in root.iterdir():
                    try:
                        stat = item.stat()
                        if stat.st_mtime >= cutoff:
                            results.append({
                                "path": str(item),
                                "type": "dir" if item.is_dir() else "file",
                                "mtime": stat.st_mtime,
                            })
                        # Also scan inside subdirs (one level)
                        if item.is_dir():
                            for sub in item.iterdir():
                                try:
                                    ss = sub.stat()
                                    if ss.st_mtime >= cutoff:
                                        results.append({
                                            "path": str(sub),
                                            "type": "dir" if sub.is_dir() else "file",
                                            "mtime": ss.st_mtime,
                                        })
                                except OSError:
                                    pass
                    except OSError:
                        pass
            except PermissionError:
                pass
        # Sort by mtime descending, limit to 100
        results.sort(key=lambda x: x["mtime"], reverse=True)
        return results[:100]

    def list_current(self) -> dict:
        """
        Full current snapshot of watched directories.
        Scans top-level items + one level of subdirectories to capture nested folders.

        Structure per watched dir:
          {
            "dirs":    [all top-level folder names],
            "files":   [top-level file names, up to 100],
            "subdirs": {"FolderName": [its immediate children names, up to 30]},
            "total":   N,
            "recent":  [names modified in last 24h],
          }
        """
        cutoff_24h = time.time() - 86400
        result = {}
        for d in self._dirs:
            root = Path(d)
            if not root.exists():
                continue
            dirs_list = []
            files_list = []
            recent_names = set()
            subdirs: dict[str, list[str]] = {}
            try:
                for item in sorted(root.iterdir(), key=lambda x: x.name.lower()):
                    name = item.name
                    if name.startswith(".") or name in _IGNORE:
                        continue
                    try:
                        mtime = item.stat().st_mtime
                        if mtime >= cutoff_24h:
                            recent_names.add(name)
                        if item.is_dir():
                            dirs_list.append(name)
                            # Scan one level into each subdir to find nested items
                            children = []
                            try:
                                for child in sorted(item.iterdir(),
                                                    key=lambda x: x.name.lower()):
                                    if child.name.startswith(".") or child.name in _IGNORE:
                                        continue
                                    suffix = "/" if child.is_dir() else ""
                                    children.append(child.name + suffix)
                                    try:
                                        if child.stat().st_mtime >= cutoff_24h:
                                            recent_names.add(child.name)
                                    except OSError:
                                        pass
                                    if len(children) >= 50:
                                        break
                            except PermissionError:
                                pass
                            if children:
                                subdirs[name] = children
                        else:
                            files_list.append(name)
                    except OSError:
                        pass
            except PermissionError:
                pass
            result[d] = {
                "dirs":    dirs_list,
                "files":   files_list[:100],
                "subdirs": subdirs,
                "total":   len(dirs_list) + len(files_list),
                "recent":  list(recent_names),
            }
        return result

    def start(self):
        if not _WATCHDOG or not self._dirs:
            return

        class Handler(FileSystemEventHandler):
            def __init__(self, q):
                self._q = q

            def on_any_event(self, event):
                # Track both files AND directories (folder creation is important)
                name = os.path.basename(str(event.src_path))
                _, ext = os.path.splitext(name)
                if name in _IGNORE or ext in _IGNORE:
                    return
                kind = "dir" if event.is_directory else "file"
                self._q.put({"path": str(event.src_path),
                             "type": event.event_type,
                             "kind": kind,
                             "ts": time.time()})

        self._observer = Observer()
        for d in self._dirs:
            if Path(d).exists():
                self._observer.schedule(Handler(self._q), d, recursive=True)
        self._observer.start()
        log.info("Watching dirs: %s", self._dirs)

    def stop(self):
        if self._observer:
            self._observer.stop()
            self._observer.join()

    def drain(self, max_events: int = 30) -> list[dict]:
        events = []
        try:
            while len(events) < max_events:
                events.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return events
