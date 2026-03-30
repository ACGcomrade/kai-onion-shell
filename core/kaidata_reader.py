"""
core/kaidata_reader.py — kaiData reading interface for external projects (e.g. me2).

This module provides a stable, well-documented API for reading all content types
stored in kaiData. It is intentionally separate from packager.py (which assembles
AI context) and store.py (which handles writes).

=============================================================================
KAIDATA LOCATION
=============================================================================
Default: /Volumes/ACGcomrade_entelechy/kaiMemoriesProject/kaiData/
  kai.db           — SQLite database (all structured data)
  screenshots/     — Raw JPEG screenshots (YYYY/MM/DD/HH-MM-SS_{app}.jpg)
  .bomber_state.json — Bomber last-run metadata

=============================================================================
DATABASE SCHEMA  (kai.db)
=============================================================================

TABLE: events
  id          INTEGER PK
  ts          REAL          — Unix timestamp
  channel     TEXT          — "app" | "browser" | "clipboard" | "screen" |
                              "file" | "terminal" | "keyboard" | "mouse" |
                              "audio" | "reader" | "system" | "init"
  summary     TEXT          — One-line human-readable description
  detail_json TEXT          — JSON blob with structured detail (nullable)
  category    TEXT          — "browsing" | "coding" | "files" | "content" |
                              "navigation" | "system" | "other"
  importance  INTEGER       — 0 (noise) → 4 (critical)
  domain      TEXT          — App name or URL domain

TABLE: checkpoints
  id              INTEGER PK
  ts              REAL
  app_name        TEXT      — Frontmost application
  window_title    TEXT      — Active window title
  url             TEXT      — Browser URL (if applicable)
  clipboard_hash  TEXT      — SHA-256 of clipboard content
  recent_commands TEXT      — JSON array of last shell commands

TABLE: ocr_snapshots
  id              INTEGER PK
  ts              REAL
  trigger         TEXT      — "init" | "app_switch" | "title_change" |
                              "fallback" | "user_ask"
  app_name        TEXT
  window_title    TEXT
  ocr_zlib        BLOB      — zlib-compressed UTF-8 screen text
  confidence      REAL      — OCR confidence 0.0–1.0
  screenshot_path TEXT      — Absolute path to JPEG in screenshots/

TABLE: vision_snapshots
  id              INTEGER PK
  ts              REAL
  app_name        TEXT
  window_title    TEXT
  description     TEXT      — LLM-generated semantic description of screen
  trigger         TEXT      — "background" | "user_ask"
  source          TEXT      — "screen" | "camera"

TABLE: audio_transcripts
  id          INTEGER PK
  ts          REAL
  trigger     TEXT          — "vad" (voice activity detected)
  app_name    TEXT
  duration_s  REAL          — Segment duration in seconds
  transcript  TEXT          — Transcribed text
  language    TEXT          — Detected language code (e.g. "zh", "en")
  confidence  REAL
  category    TEXT          — "communication" | "coding"

TABLE: file_descriptions  (media files analyzed by watcher)
  id            INTEGER PK
  ts            REAL
  path          TEXT UNIQUE — Absolute file path
  file_type     TEXT        — "image" | "video" | "audio"
  description   TEXT        — Detailed LLM description
  summary       TEXT        — Short one-line summary
  duration_s    REAL        — Duration for video/audio (0 for images)
  transcript    TEXT        — Audio/video transcript if available
  thumbnail_b64 TEXT        — Base64 JPEG thumbnail (may be empty)

TABLE: reader_refs  (local file references — no content copy)
  id              INTEGER PK
  ts_first_seen   REAL      — When first discovered
  ts_last_seen    REAL      — Last ping confirming file exists
  ts_analyzed     REAL      — When LLM analysis was last performed
  path            TEXT UNIQUE — Absolute path on disk
  file_type       TEXT        — "image" | "video" | "audio" | "text" |
                                "pdf" | "document" | "dir" | "other"
  size_bytes      INTEGER
  mtime           REAL        — File modification time
  description     TEXT        — LLM description (may be empty for dir/other)
  summary         TEXT        — Short summary
  alive           INTEGER     — 1 = file exists, 0 = file deleted (kept 24h)

TABLE: fs_snapshots  (directory listings, capped at 3 most recent)
  id          INTEGER PK
  ts          REAL
  data_json   TEXT          — JSON: {"~/Desktop": {"dirs": [...], "files": [...],
                              "subdirs": {...}, "recent": [...], "total": N}}

=============================================================================
SCREENSHOTS
=============================================================================
Path pattern: kaiData/screenshots/YYYY/MM/DD/HH-MM-SS_{app_name}.jpg
Each screenshot corresponds to an ocr_snapshots row via screenshot_path column.
Quality: JPEG 75% (lossy), max resolution = system screen resolution.

=============================================================================
USAGE (Python)
=============================================================================

    from core.kaidata_reader import KaiDataReader

    reader = KaiDataReader()          # uses default kaiData path
    # or:
    reader = KaiDataReader("/custom/path/to/kaiData")

    # Time range queries
    events   = reader.events(hours=24)
    ocr      = reader.ocr_snapshots(hours=2)
    vision   = reader.vision_snapshots(hours=1)
    audio    = reader.audio_transcripts(hours=4)
    refs     = reader.reader_refs(alive_only=True)
    media    = reader.file_descriptions()
    fs       = reader.latest_fs_snapshot()
    cp       = reader.latest_checkpoint()
    screens  = reader.screenshot_paths(hours=2)

    # Filtered queries
    events   = reader.events(hours=1, channel="keyboard")
    events   = reader.events(hours=24, category="coding")
    vision   = reader.vision_snapshots(hours=1, source="camera")
    refs     = reader.reader_refs(file_type="image")

    # OCR text (decompressed)
    snap     = reader.ocr_snapshots(hours=1)
    for s in snap:
        print(s["ocr_text"])   # already decompressed

    # Storage stats
    stats    = reader.storage_stats()
    # → {"db_bytes": 1234567, "screenshots_bytes": 9876543,
    #    "total_bytes": ..., "db_path": "...", "kaidata_dir": "..."}

=============================================================================
DATA FRESHNESS
=============================================================================
- events:         watcher writes every few seconds (level-dependent)
- checkpoints:    written every 5 minutes (10 min when idle)
- ocr_snapshots:  written on window title change + periodic fallback
- vision_snapshots: written every 10–30 seconds for active apps
- audio_transcripts: written after each voice segment ends (VAD)
- reader_refs:    scanned every 5 minutes, pinged every 30 minutes
- file_descriptions: scanned every 5 minutes

=============================================================================
BOMBER (retention policy)
=============================================================================
The bomber runs every 6 hours and enforces storage_gb budget (set via web UI).
Retention pyramid (older data is downsampled):
  0–1h   : keep all
  1h–24h : keep 1 per 5 min
  24h–7d : keep 1 per hour
  7d–30d : keep 1 per day
  >30d   : keep 1 per week
reader_refs, checkpoints, file_descriptions are never downsampled by bomber.
"""
from __future__ import annotations
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Optional


class KaiDataReader:
    """
    Read-only interface to kaiData.
    All methods return plain dicts/lists — no ORM, no side effects.
    Thread-safe: opens a fresh read-only connection per instance.
    """

    def __init__(self, kaidata_path: str = ""):
        from core.kaidata import get_kaidata_dir, get_db_path
        self._kaidata_dir = get_kaidata_dir(kaidata_path)
        self._db_path = get_db_path(kaidata_path)
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=5.0,
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Events ─────────────────────────────────────────────────────────────

    def events(self, hours: float = 1.0, limit: int = 500,
               channel: str = "", category: str = "",
               min_importance: int = 0,
               ts_from: Optional[float] = None,
               ts_to: Optional[float] = None) -> list[dict]:
        """
        Return events from the last `hours` hours.
        Optionally filter by channel, category, or minimum importance.
        Pass ts_from/ts_to (Unix timestamps) to query an absolute time range.
        Results are oldest-first.
        """
        since = ts_from if ts_from is not None else time.time() - hours * 3600
        params: list[Any] = [since]
        where = "ts >= ?"
        if ts_to is not None:
            where += " AND ts <= ?"; params.append(ts_to)
        if channel:
            where += " AND channel = ?"; params.append(channel)
        if category:
            where += " AND category = ?"; params.append(category)
        if min_importance > 0:
            where += " AND importance >= ?"; params.append(min_importance)
        params.append(limit)

        rows = self._get_conn().execute(
            f"SELECT * FROM events WHERE {where} ORDER BY ts ASC LIMIT ?",
            params,
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            if d.get("detail_json"):
                import json
                try:
                    d["detail"] = json.loads(d["detail_json"])
                except Exception:
                    d["detail"] = None
            else:
                d["detail"] = None
            result.append(d)
        return result

    # ── Checkpoints ────────────────────────────────────────────────────────

    def checkpoints(self, hours: float = 24.0, limit: int = 100,
                    ts_from: Optional[float] = None,
                    ts_to: Optional[float] = None) -> list[dict]:
        """Return periodic state snapshots."""
        since = ts_from if ts_from is not None else time.time() - hours * 3600
        params: list[Any] = [since]
        where = "ts >= ?"
        if ts_to is not None:
            where += " AND ts <= ?"; params.append(ts_to)
        params.append(limit)
        rows = self._get_conn().execute(
            f"SELECT * FROM checkpoints WHERE {where} ORDER BY ts ASC LIMIT ?",
            params,
        ).fetchall()
        import json
        result = []
        for r in rows:
            d = dict(r)
            d["recent_commands"] = json.loads(d.get("recent_commands") or "[]")
            result.append(d)
        return result

    def latest_checkpoint(self) -> Optional[dict]:
        """Return the most recent checkpoint."""
        row = self._get_conn().execute(
            "SELECT * FROM checkpoints ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        import json
        d = dict(row)
        d["recent_commands"] = json.loads(d.get("recent_commands") or "[]")
        return d

    # ── OCR snapshots ──────────────────────────────────────────────────────

    def ocr_snapshots(self, hours: float = 2.0, limit: int = 50,
                      decompress: bool = True,
                      ts_from: Optional[float] = None,
                      ts_to: Optional[float] = None) -> list[dict]:
        """
        Return OCR snapshots.
        If decompress=True (default), ocr_zlib is decoded to ocr_text string.
        If decompress=False, raw bytes are returned as ocr_zlib.
        Pass ts_from/ts_to (Unix timestamps) to query an absolute time range.
        """
        since = ts_from if ts_from is not None else time.time() - hours * 3600
        params: list[Any] = [since]
        where = "ts >= ?"
        if ts_to is not None:
            where += " AND ts <= ?"; params.append(ts_to)
        params.append(limit)
        rows = self._get_conn().execute(
            f"SELECT * FROM ocr_snapshots WHERE {where} ORDER BY ts ASC LIMIT ?",
            params,
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if decompress and d.get("ocr_zlib"):
                try:
                    d["ocr_text"] = zlib.decompress(d["ocr_zlib"]).decode("utf-8", errors="replace")
                except Exception:
                    d["ocr_text"] = ""
                del d["ocr_zlib"]
            result.append(d)
        return result

    # ── Vision snapshots ───────────────────────────────────────────────────

    def vision_snapshots(self, hours: float = 1.0, limit: int = 50,
                         source: str = "",
                         ts_from: Optional[float] = None,
                         ts_to: Optional[float] = None) -> list[dict]:
        """
        Return vision LLM descriptions.
        source: "" = all, "screen" = screen only, "camera" = camera only.
        Pass ts_from/ts_to (Unix timestamps) to query an absolute time range.
        """
        since = ts_from if ts_from is not None else time.time() - hours * 3600
        params: list[Any] = [since]
        where = "ts >= ?"
        if ts_to is not None:
            where += " AND ts <= ?"; params.append(ts_to)
        if source:
            where += " AND source = ?"; params.append(source)
        params.append(limit)
        rows = self._get_conn().execute(
            f"SELECT * FROM vision_snapshots WHERE {where} ORDER BY ts ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Audio transcripts ──────────────────────────────────────────────────

    def audio_transcripts(self, hours: float = 4.0, limit: int = 100,
                          ts_from: Optional[float] = None,
                          ts_to: Optional[float] = None) -> list[dict]:
        """Return microphone transcripts."""
        since = ts_from if ts_from is not None else time.time() - hours * 3600
        params: list[Any] = [since]
        where = "ts >= ?"
        if ts_to is not None:
            where += " AND ts <= ?"; params.append(ts_to)
        params.append(limit)
        rows = self._get_conn().execute(
            f"SELECT * FROM audio_transcripts WHERE {where} ORDER BY ts ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Reader refs ────────────────────────────────────────────────────────

    def reader_refs(self, alive_only: bool = True, file_type: str = "",
                    limit: int = 1000,
                    ts_from: Optional[float] = None,
                    ts_to: Optional[float] = None) -> list[dict]:
        """
        Return local file references.
        alive_only=True excludes deleted files (alive=0).
        file_type: filter by type ("image", "video", "audio", "text", "pdf",
                   "document", "dir", "other"). "" = all types.
        ts_from/ts_to filter on ts_last_seen (Unix timestamps).
        """
        params: list[Any] = []
        clauses = []
        if alive_only:
            clauses.append("alive = 1")
        if file_type:
            clauses.append("file_type = ?"); params.append(file_type)
        if ts_from is not None:
            clauses.append("ts_last_seen >= ?"); params.append(ts_from)
        if ts_to is not None:
            clauses.append("ts_last_seen <= ?"); params.append(ts_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._get_conn().execute(
            f"SELECT * FROM reader_refs {where} ORDER BY ts_last_seen DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── File descriptions (watcher-analyzed media) ─────────────────────────

    def file_descriptions(self, hours: float = 0, limit: int = 200,
                          file_type: str = "",
                          ts_from: Optional[float] = None,
                          ts_to: Optional[float] = None) -> list[dict]:
        """
        Return pre-analyzed media file descriptions.
        hours=0 means return all (no time filter).
        Pass ts_from/ts_to (Unix timestamps) for an absolute time range.
        """
        params: list[Any] = []
        clauses = []
        if ts_from is not None:
            clauses.append("ts >= ?"); params.append(ts_from)
        elif hours > 0:
            clauses.append("ts >= ?"); params.append(time.time() - hours * 3600)
        if ts_to is not None:
            clauses.append("ts <= ?"); params.append(ts_to)
        if file_type:
            clauses.append("file_type = ?"); params.append(file_type)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._get_conn().execute(
            f"SELECT * FROM file_descriptions {where} ORDER BY ts DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Filesystem snapshots ───────────────────────────────────────────────

    def latest_fs_snapshot(self) -> Optional[dict]:
        """Return the most recent filesystem directory listing snapshot."""
        row = self._get_conn().execute(
            "SELECT ts, data_json FROM fs_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        import json
        return {"ts": row["ts"], "data": json.loads(row["data_json"])}

    # ── Screenshots ────────────────────────────────────────────────────────

    def screenshot_paths(self, hours: float = 2.0) -> list[dict]:
        """
        Return screenshot file paths for captures in the last `hours` hours.
        Each result: {"ts": float, "path": str, "app_name": str, "exists": bool}
        """
        since = time.time() - hours * 3600
        rows = self._get_conn().execute(
            """SELECT ts, app_name, window_title, screenshot_path
               FROM ocr_snapshots
               WHERE ts >= ? AND screenshot_path != ''
               ORDER BY ts ASC""",
            (since,),
        ).fetchall()
        result = []
        for r in rows:
            p = r["screenshot_path"] or ""
            result.append({
                "ts": r["ts"],
                "app_name": r["app_name"] or "",
                "window_title": r["window_title"] or "",
                "path": p,
                "exists": Path(p).exists() if p else False,
            })
        return result

    def list_screenshot_files(self, date: str = "") -> list[Path]:
        """
        List JPEG screenshot files under kaiData/screenshots/.
        date: "YYYY-MM-DD" to filter to one day. "" = list all.
        """
        screens_dir = self._kaidata_dir / "screenshots"
        if not screens_dir.exists():
            return []
        if date:
            parts = date.split("-")
            if len(parts) == 3:
                target = screens_dir / parts[0] / parts[1] / parts[2]
                if target.exists():
                    return sorted(target.glob("*.jpg"))
                return []
        return sorted(screens_dir.rglob("*.jpg"))

    # ── Storage stats ──────────────────────────────────────────────────────

    def storage_stats(self) -> dict:
        """
        Return disk usage stats for kaiData.
        Result keys: db_bytes, screenshots_bytes, total_bytes,
                     db_path, kaidata_dir, row_counts.
        """
        db_bytes = self._db_path.stat().st_size if self._db_path.exists() else 0
        screens_dir = self._kaidata_dir / "screenshots"
        screens_bytes = sum(
            f.stat().st_size for f in screens_dir.rglob("*.jpg") if f.is_file()
        ) if screens_dir.exists() else 0

        row_counts = {}
        conn = self._get_conn()
        for tbl in ["events", "checkpoints", "ocr_snapshots", "vision_snapshots",
                    "audio_transcripts", "file_descriptions", "reader_refs", "fs_snapshots"]:
            try:
                row_counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                row_counts[tbl] = -1

        return {
            "db_bytes": db_bytes,
            "screenshots_bytes": screens_bytes,
            "total_bytes": db_bytes + screens_bytes,
            "db_path": str(self._db_path),
            "kaidata_dir": str(self._kaidata_dir),
            "row_counts": row_counts,
        }

    # ── Time range summary (for me2 — personality/activity graph) ──────────

    def activity_summary(self, hours: float = 24.0) -> dict:
        """
        Return a structured activity summary for the last `hours` hours.
        Useful for building user behavior graphs in me2.

        Returns:
          apps_used:    list of {app_name, count, first_seen, last_seen}
          urls_visited: list of {domain, count, first_seen}
          typed_text:   list of {ts, text, app_name}   (keyboard events)
          voice_clips:  list of {ts, transcript, duration_s, language}
          files_seen:   list of {path, file_type, summary}  (reader refs)
          screen_text:  list of {ts, app_name, ocr_text}  (OCR snapshots)
          time_range:   {"from": ts, "to": ts, "hours": float}
        """
        now = time.time()
        since = now - hours * 3600
        conn = self._get_conn()

        # Apps used
        rows = conn.execute(
            """SELECT detail_json, ts FROM events
               WHERE ts >= ? AND channel = 'app' ORDER BY ts ASC""",
            (since,),
        ).fetchall()
        import json
        apps_map: dict[str, dict] = {}
        for r in rows:
            d = json.loads(r["detail_json"] or "{}")
            app = d.get("to") or ""
            if not app:
                continue
            if app not in apps_map:
                apps_map[app] = {"app_name": app, "count": 0, "first_seen": r["ts"], "last_seen": r["ts"]}
            apps_map[app]["count"] += 1
            apps_map[app]["last_seen"] = r["ts"]
        apps_used = sorted(apps_map.values(), key=lambda x: -x["count"])

        # URLs visited
        url_rows = conn.execute(
            """SELECT domain, ts FROM events
               WHERE ts >= ? AND channel = 'browser' ORDER BY ts ASC""",
            (since,),
        ).fetchall()
        url_map: dict[str, dict] = {}
        for r in url_rows:
            d = r["domain"] or ""
            if d:
                if d not in url_map:
                    url_map[d] = {"domain": d, "count": 0, "first_seen": r["ts"]}
                url_map[d]["count"] += 1
        urls_visited = sorted(url_map.values(), key=lambda x: -x["count"])

        # Typed text
        kb_rows = conn.execute(
            """SELECT ts, detail_json, domain FROM events
               WHERE ts >= ? AND channel = 'keyboard' ORDER BY ts ASC""",
            (since,),
        ).fetchall()
        typed_text = []
        for r in kb_rows:
            d = json.loads(r["detail_json"] or "{}")
            typed_text.append({
                "ts": r["ts"],
                "text": d.get("text", ""),
                "app_name": d.get("app") or r["domain"] or "",
            })

        # Voice clips
        audio_rows = conn.execute(
            """SELECT ts, transcript, duration_s, language FROM audio_transcripts
               WHERE ts >= ? ORDER BY ts ASC""",
            (since,),
        ).fetchall()
        voice_clips = [dict(r) for r in audio_rows]

        # Files seen (reader refs touched recently)
        ref_rows = conn.execute(
            """SELECT path, file_type, summary FROM reader_refs
               WHERE ts_last_seen >= ? AND alive = 1
               ORDER BY ts_last_seen DESC LIMIT 200""",
            (since,),
        ).fetchall()
        files_seen = [dict(r) for r in ref_rows]

        # Screen text (OCR)
        ocr_rows = conn.execute(
            """SELECT ts, app_name, window_title, ocr_zlib FROM ocr_snapshots
               WHERE ts >= ? ORDER BY ts ASC LIMIT 20""",
            (since,),
        ).fetchall()
        screen_text = []
        for r in ocr_rows:
            try:
                text = zlib.decompress(r["ocr_zlib"]).decode("utf-8", errors="replace") if r["ocr_zlib"] else ""
            except Exception:
                text = ""
            screen_text.append({
                "ts": r["ts"],
                "app_name": r["app_name"] or "",
                "window_title": r["window_title"] or "",
                "ocr_text": text,
            })

        return {
            "apps_used": apps_used,
            "urls_visited": urls_visited,
            "typed_text": typed_text,
            "voice_clips": voice_clips,
            "files_seen": files_seen,
            "screen_text": screen_text,
            "time_range": {"from": since, "to": now, "hours": hours},
        }

    # ── Data change ratio (external API for me2 / bomber) ─────────────────

    def change_ratio(self, ts_since: float,
                     tables: Optional[list[str]] = None) -> dict:
        """
        Return the cumulative data change ratio for each table:
            ratio = rows_since_ts / total_rows

        This is the canonical signal exported to me2 (profile update trigger)
        and to bomber (maintenance trigger).  Both consumers can call this
        instead of separately computing n_new and n_total.

        Args:
            ts_since:  Unix timestamp — rows added after this point are "new"
            tables:    Tables to check.  Defaults to the four bomber tables:
                       events, ocr_snapshots, vision_snapshots, audio_transcripts
                       (reader_refs / checkpoints / file_descriptions excluded —
                       they have no time-ordered ts column or are never downsampled)

        Returns dict per table:  {
            "events":          {"new": int, "total": int, "ratio": float},
            "ocr_snapshots":   {...},
            "vision_snapshots":{...},
            "audio_transcripts":{...},
            "combined":        {"new": int, "total": int, "ratio": float},
        }
        The "combined" key is the weighted-average ratio across all requested
        tables (equal weight).
        """
        if tables is None:
            tables = ["events", "ocr_snapshots", "vision_snapshots", "audio_transcripts"]

        conn = self._get_conn()
        result: dict = {}
        total_new, total_all = 0, 0

        for tbl in tables:
            try:
                n_total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                n_new = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE ts >= ?", (ts_since,)
                ).fetchone()[0]
            except Exception:
                result[tbl] = {"new": 0, "total": 0, "ratio": 0.0}
                continue
            ratio = n_new / max(n_total, 1)
            result[tbl] = {"new": n_new, "total": n_total, "ratio": ratio}
            total_new += n_new
            total_all += n_total

        result["combined"] = {
            "new": total_new,
            "total": total_all,
            "ratio": total_new / max(total_all, 1),
        }
        return result
