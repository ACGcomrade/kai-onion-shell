"""
SQLite store — three tables:
  events       : every change (app switch, clipboard, file, terminal, screen)
  checkpoints  : periodic full state snapshot (no OCR text)
  ocr_snapshots: screen text, only when window changes or user asks
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Optional

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    channel     TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    detail_json TEXT,
    category    TEXT    DEFAULT 'other',
    importance  INTEGER DEFAULT 2,
    domain      TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS checkpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    app_name        TEXT,
    window_title    TEXT,
    url             TEXT,
    clipboard_hash  TEXT,
    recent_commands TEXT
);
CREATE INDEX IF NOT EXISTS idx_cp_ts ON checkpoints(ts);

CREATE TABLE IF NOT EXISTS ocr_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    trigger      TEXT NOT NULL,
    app_name     TEXT,
    window_title TEXT,
    ocr_zlib     BLOB,
    confidence   REAL
);
CREATE INDEX IF NOT EXISTS idx_ocr_ts ON ocr_snapshots(ts);

CREATE TABLE IF NOT EXISTS fs_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    data_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vision_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    app_name    TEXT,
    window_title TEXT,
    description TEXT NOT NULL,
    trigger     TEXT DEFAULT 'background',
    source      TEXT DEFAULT 'screen'
);
CREATE INDEX IF NOT EXISTS idx_vision_ts ON vision_snapshots(ts);

CREATE TABLE IF NOT EXISTS audio_transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    trigger     TEXT NOT NULL,
    app_name    TEXT,
    duration_s  REAL,
    transcript  TEXT NOT NULL,
    language    TEXT DEFAULT '',
    confidence  REAL DEFAULT 0.0,
    category    TEXT DEFAULT 'communication'
);
CREATE INDEX IF NOT EXISTS idx_audio_ts ON audio_transcripts(ts);

CREATE TABLE IF NOT EXISTS file_descriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    path          TEXT NOT NULL UNIQUE,
    file_type     TEXT NOT NULL,
    description   TEXT NOT NULL,
    summary       TEXT DEFAULT '',
    duration_s    REAL DEFAULT 0.0,
    transcript    TEXT DEFAULT '',
    thumbnail_b64 TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fd_path ON file_descriptions(path);
CREATE INDEX IF NOT EXISTS idx_fd_ts ON file_descriptions(ts);

-- Reader layer: references to local files (no copy, just metadata + description)
CREATE TABLE IF NOT EXISTS reader_refs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_first_seen   REAL NOT NULL,
    ts_last_seen    REAL NOT NULL,
    ts_analyzed     REAL,
    path            TEXT NOT NULL UNIQUE,
    file_type       TEXT NOT NULL,
    size_bytes      INTEGER,
    mtime           REAL,
    description     TEXT DEFAULT '',
    summary         TEXT DEFAULT '',
    alive           INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_reader_path ON reader_refs(path);
CREATE INDEX IF NOT EXISTS idx_reader_ts ON reader_refs(ts_last_seen);
"""


class Store:
    def __init__(self, db_path: Path, readonly: bool = False):
        self._readonly = readonly
        if readonly:
            # Read-only URI — no WAL writes, no locking conflicts
            self._path = f"file:{db_path}?mode=ro"
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._path = str(db_path)
        self._local = threading.local()
        if not readonly:
            self._conn().executescript(DDL)
            self._conn().commit()
        # Idempotent migrations for existing DBs
        _migrations = [
            "ALTER TABLE events ADD COLUMN category TEXT DEFAULT 'other'",
            "ALTER TABLE events ADD COLUMN importance INTEGER DEFAULT 2",
            "ALTER TABLE events ADD COLUMN domain TEXT DEFAULT ''",
            "ALTER TABLE vision_snapshots ADD COLUMN trigger TEXT DEFAULT 'background'",
            "ALTER TABLE vision_snapshots ADD COLUMN source TEXT DEFAULT 'screen'",
            # file_descriptions table added in v2
            """CREATE TABLE IF NOT EXISTS file_descriptions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL NOT NULL,
                path          TEXT NOT NULL UNIQUE,
                file_type     TEXT NOT NULL,
                description   TEXT NOT NULL,
                summary       TEXT DEFAULT '',
                duration_s    REAL DEFAULT 0.0,
                transcript    TEXT DEFAULT '',
                thumbnail_b64 TEXT DEFAULT ''
            )""",
            "CREATE INDEX IF NOT EXISTS idx_fd_path ON file_descriptions(path)",
            "CREATE INDEX IF NOT EXISTS idx_fd_ts ON file_descriptions(ts)",
            # reader_refs table (v4)
            """CREATE TABLE IF NOT EXISTS reader_refs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_first_seen   REAL NOT NULL,
                ts_last_seen    REAL NOT NULL,
                ts_analyzed     REAL,
                path            TEXT NOT NULL UNIQUE,
                file_type       TEXT NOT NULL,
                size_bytes      INTEGER,
                mtime           REAL,
                description     TEXT DEFAULT '',
                summary         TEXT DEFAULT '',
                alive           INTEGER DEFAULT 1
            )""",
            "CREATE INDEX IF NOT EXISTS idx_reader_path ON reader_refs(path)",
            "CREATE INDEX IF NOT EXISTS idx_reader_ts ON reader_refs(ts_last_seen)",
            # screenshot_path column on ocr_snapshots (v4)
            "ALTER TABLE ocr_snapshots ADD COLUMN screenshot_path TEXT DEFAULT ''",
            # presence + change quality columns (v5)
            "ALTER TABLE ocr_snapshots ADD COLUMN screen_change_score REAL DEFAULT 1.0",
            "ALTER TABLE ocr_snapshots ADD COLUMN is_away INTEGER DEFAULT 0",
        ]
        conn = self._conn()
        for sql in _migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "c"):
            uri = self._readonly
            self._local.c = sqlite3.connect(self._path, check_same_thread=False,
                                             timeout=5.0, uri=uri)
            self._local.c.row_factory = sqlite3.Row
        return self._local.c

    # ── Write ──────────────────────────────────────────────────────────────

    def log_event(self, channel: str, summary: str, detail: dict | None = None,
                  category: str = "", importance: int = 2, domain: str = ""):
        self._conn().execute(
            "INSERT INTO events (ts, channel, summary, detail_json, category, importance, domain) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.time(), channel, summary, json.dumps(detail) if detail else None,
             category or "other", importance, domain),
        )
        self._conn().commit()

    def save_checkpoint(self, app_name: str, window_title: str,
                        clipboard_hash: str, recent_commands: list[str],
                        url: str = ""):
        self._conn().execute(
            """INSERT INTO checkpoints
               (ts, app_name, window_title, url, clipboard_hash, recent_commands)
               VALUES (?,?,?,?,?,?)""",
            (time.time(), app_name, window_title, url,
             clipboard_hash, json.dumps(recent_commands)),
        )
        self._conn().commit()

    def save_ocr(self, trigger: str, app_name: str, window_title: str,
                 ocr_text: str, confidence: float, screenshot_path: str = "",
                 screen_change_score: float = 1.0, is_away: int = 0):
        compressed = zlib.compress(ocr_text.encode("utf-8", errors="replace"), level=6)
        self._conn().execute(
            """INSERT INTO ocr_snapshots
               (ts, trigger, app_name, window_title, ocr_zlib, confidence,
                screenshot_path, screen_change_score, is_away)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (time.time(), trigger, app_name, window_title, compressed, confidence,
             screenshot_path, screen_change_score, is_away),
        )
        self._conn().commit()

    # ── Read ───────────────────────────────────────────────────────────────

    def recent_events(self, minutes: float = 60.0, limit: int = 200) -> list[dict]:
        since = time.time() - minutes * 60
        rows = self._conn().execute(
            "SELECT ts, channel, summary, detail_json, category, importance, domain FROM events "
            "WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
            (since, limit),
        ).fetchall()
        return [{"ts": r["ts"], "channel": r["channel"], "summary": r["summary"],
                 "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
                 "category": r["category"] or "other",
                 "importance": r["importance"] if r["importance"] is not None else 2,
                 "domain": r["domain"] or ""}
                for r in rows]

    def latest_checkpoint(self) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT * FROM checkpoints ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "ts": row["ts"],
            "app_name": row["app_name"] or "",
            "window_title": row["window_title"] or "",
            "url": row["url"] or "",
            "clipboard_hash": row["clipboard_hash"] or "",
            "recent_commands": json.loads(row["recent_commands"] or "[]"),
        }

    def latest_ocr(self, max_age_minutes: float = 30.0) -> Optional[dict]:
        since = time.time() - max_age_minutes * 60
        row = self._conn().execute(
            "SELECT * FROM ocr_snapshots WHERE ts >= ? ORDER BY ts DESC LIMIT 1",
            (since,),
        ).fetchone()
        if not row:
            return None
        ocr_text = zlib.decompress(row["ocr_zlib"]).decode("utf-8", errors="replace")
        return {
            "ts": row["ts"],
            "trigger": row["trigger"],
            "app_name": row["app_name"] or "",
            "window_title": row["window_title"] or "",
            "ocr_text": ocr_text,
            "confidence": row["confidence"] or 0.0,
        }

    def two_latest_ocr(self) -> list[dict]:
        """Return [older, newer] for computing delta."""
        rows = self._conn().execute(
            "SELECT * FROM ocr_snapshots ORDER BY ts DESC LIMIT 2"
        ).fetchall()
        result = []
        for row in reversed(rows):
            ocr_text = zlib.decompress(row["ocr_zlib"]).decode("utf-8", errors="replace")
            result.append({
                "ts": row["ts"],
                "app_name": row["app_name"] or "",
                "window_title": row["window_title"] or "",
                "ocr_text": ocr_text,
                "confidence": row["confidence"] or 0.0,
            })
        return result

    def event_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def last_event_ts(self) -> Optional[float]:
        row = self._conn().execute("SELECT MAX(ts) FROM events").fetchone()
        return row[0] if row else None

    def save_vision(self, app_name: str, window_title: str, description: str,
                    trigger: str = "background", source: str = "screen"):
        """Save a vision LLM description of the current screen or camera."""
        conn = self._conn()
        conn.execute(
            """INSERT INTO vision_snapshots (ts, app_name, window_title, description, trigger, source)
               VALUES (?,?,?,?,?,?)""",
            (time.time(), app_name, window_title, description, trigger, source),
        )
        # Keep only the 50 most recent vision snapshots (more history for video sequences)
        conn.execute("""DELETE FROM vision_snapshots WHERE id NOT IN (
            SELECT id FROM vision_snapshots ORDER BY ts DESC LIMIT 50)""")
        conn.commit()

    def recent_vision(self, minutes: float = 30.0, limit: int = 5,
                      source: str = "screen") -> list[dict]:
        """Return recent vision descriptions, oldest first. source='screen'|'camera'|'all'"""
        since = time.time() - minutes * 60
        if source == "all":
            rows = self._conn().execute(
                """SELECT ts, app_name, window_title, description, trigger, source
                   FROM vision_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT ?""",
                (since, limit),
            ).fetchall()
        else:
            rows = self._conn().execute(
                """SELECT ts, app_name, window_title, description, trigger, source
                   FROM vision_snapshots WHERE ts >= ? AND source=? ORDER BY ts ASC LIMIT ?""",
                (since, source, limit),
            ).fetchall()
        return [{"ts": r["ts"], "app_name": r["app_name"] or "",
                 "window_title": r["window_title"] or "",
                 "description": r["description"],
                 "trigger": r["trigger"] or "background",
                 "source": r["source"] or "screen"} for r in rows]

    def save_audio(self, trigger: str, app_name: str, transcript: str,
                   duration_s: float = 0.0, language: str = "",
                   confidence: float = 0.0, category: str = "communication"):
        """Save a microphone transcription."""
        conn = self._conn()
        conn.execute(
            """INSERT INTO audio_transcripts
               (ts, trigger, app_name, duration_s, transcript, language, confidence, category)
               VALUES (?,?,?,?,?,?,?,?)""",
            (time.time(), trigger, app_name, duration_s,
             transcript, language, confidence, category),
        )
        # Keep only 200 most recent transcripts
        conn.execute("""DELETE FROM audio_transcripts WHERE id NOT IN (
            SELECT id FROM audio_transcripts ORDER BY ts DESC LIMIT 200)""")
        conn.commit()

    def recent_audio(self, minutes: float = 60.0, limit: int = 20) -> list[dict]:
        """Return recent audio transcripts, oldest first."""
        since = time.time() - minutes * 60
        rows = self._conn().execute(
            """SELECT ts, trigger, app_name, duration_s, transcript, language, confidence, category
               FROM audio_transcripts WHERE ts >= ? ORDER BY ts ASC LIMIT ?""",
            (since, limit),
        ).fetchall()
        return [{"ts": r["ts"], "trigger": r["trigger"],
                 "app_name": r["app_name"] or "",
                 "duration_s": r["duration_s"] or 0.0,
                 "transcript": r["transcript"],
                 "language": r["language"] or "",
                 "confidence": r["confidence"] or 0.0,
                 "category": r["category"] or "communication"} for r in rows]

    def latest_user_ask_vision(self, max_age_seconds: float = 30.0) -> Optional[dict]:
        """Return the most recent user_ask vision snapshot if within max_age_seconds."""
        since = time.time() - max_age_seconds
        row = self._conn().execute(
            """SELECT ts, app_name, window_title, description, trigger
               FROM vision_snapshots WHERE trigger='user_ask' AND ts >= ?
               ORDER BY ts DESC LIMIT 1""",
            (since,),
        ).fetchone()
        if not row:
            return None
        return {"ts": row["ts"], "app_name": row["app_name"] or "",
                "window_title": row["window_title"] or "",
                "description": row["description"],
                "trigger": row["trigger"]}

    def save_fs_state(self, data: dict):
        """Save a full filesystem snapshot (current dir listing)."""
        conn = self._conn()
        conn.execute("INSERT INTO fs_snapshots (ts, data_json) VALUES (?,?)",
                     (time.time(), json.dumps(data)))
        # Keep only the 3 most recent snapshots
        conn.execute("""DELETE FROM fs_snapshots WHERE id NOT IN (
            SELECT id FROM fs_snapshots ORDER BY ts DESC LIMIT 3)""")
        conn.commit()

    def latest_fs_state(self) -> Optional[dict]:
        """Return the most recent filesystem snapshot."""
        row = self._conn().execute(
            "SELECT ts, data_json FROM fs_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {"ts": row["ts"], "data": json.loads(row["data_json"])}

    # ── File descriptions ─────────────────────────────────────────────────

    def save_file_description(self, path: str, file_type: str, description: str,
                              summary: str = "", duration_s: float = 0.0,
                              transcript: str = ""):
        """Upsert a media file description (keyed by path).

        Opens a fresh connection each time rather than relying on threading.local(),
        because this is called after run_in_executor() where the cached connection
        may be in an inconsistent WAL state.
        """
        import sqlite3 as _sq3
        # Retry up to 3 times on transient lock/IO errors
        for attempt in range(3):
            try:
                conn = _sq3.connect(self._path, timeout=10.0)
                conn.execute(
                    """INSERT INTO file_descriptions
                       (ts, path, file_type, description, summary, duration_s, transcript)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                           ts=excluded.ts,
                           file_type=excluded.file_type,
                           description=excluded.description,
                           summary=excluded.summary,
                           duration_s=excluded.duration_s,
                           transcript=excluded.transcript""",
                    (time.time(), path, file_type, description, summary,
                     duration_s, transcript),
                )
                conn.commit()
                conn.close()
                return
            except Exception as e:
                if attempt < 2:
                    import time as _t; _t.sleep(0.5)
                else:
                    raise

    def get_file_description(self, path: str) -> Optional[dict]:
        """Get a media file description by path."""
        row = self._conn().execute(
            "SELECT * FROM file_descriptions WHERE path=? LIMIT 1",
            (path,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"], "ts": row["ts"], "path": row["path"],
            "file_type": row["file_type"], "description": row["description"],
            "summary": row["summary"] or "",
            "duration_s": row["duration_s"] or 0.0,
            "transcript": row["transcript"] or "",
        }

    def recent_file_descriptions(self, minutes: float = 60.0,
                                  limit: int = 20) -> list[dict]:
        """Return recently analyzed media files, oldest first."""
        since = time.time() - minutes * 60
        rows = self._conn().execute(
            """SELECT * FROM file_descriptions
               WHERE ts >= ? ORDER BY ts ASC LIMIT ?""",
            (since, limit),
        ).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "path": r["path"],
                 "file_type": r["file_type"], "description": r["description"],
                 "summary": r["summary"] or "",
                 "duration_s": r["duration_s"] or 0.0,
                 "transcript": r["transcript"] or ""} for r in rows]

    def prune_file_descriptions(self):
        """Delete entries for files that no longer exist AND were seen > 7 days ago."""
        import os as _os
        cutoff = time.time() - 7 * 86400
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, path, ts FROM file_descriptions"
        ).fetchall()
        to_delete = []
        for row in rows:
            if not _os.path.exists(row["path"]) and row["ts"] < cutoff:
                to_delete.append(row["id"])
        if to_delete:
            conn.execute(
                f"DELETE FROM file_descriptions WHERE id IN ({','.join('?' * len(to_delete))})",
                to_delete,
            )
            conn.commit()

    # ── Reader refs ────────────────────────────────────────────────────────

    def save_reader_ref(self, path: str, file_type: str,
                        size_bytes: int = 0, mtime: float = 0.0,
                        description: str = "", summary: str = ""):
        """Upsert a reader reference (local file path + metadata)."""
        now = time.time()
        conn = self._conn()
        conn.execute(
            """INSERT INTO reader_refs
               (ts_first_seen, ts_last_seen, ts_analyzed, path, file_type,
                size_bytes, mtime, description, summary, alive)
               VALUES (?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(path) DO UPDATE SET
                   ts_last_seen = excluded.ts_last_seen,
                   ts_analyzed  = CASE WHEN excluded.description != '' THEN excluded.ts_analyzed ELSE ts_analyzed END,
                   file_type    = excluded.file_type,
                   size_bytes   = excluded.size_bytes,
                   mtime        = excluded.mtime,
                   description  = CASE WHEN excluded.description != '' THEN excluded.description ELSE description END,
                   summary      = CASE WHEN excluded.summary != '' THEN excluded.summary ELSE summary END,
                   alive        = 1""",
            (now, now, now if description else None, path, file_type,
             size_bytes, mtime, description, summary),
        )
        conn.commit()

    def get_reader_ref(self, path: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT * FROM reader_refs WHERE path=? LIMIT 1", (path,)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def ping_reader_refs(self) -> tuple[int, int]:
        """Check all alive=1 refs, mark missing as alive=0. Returns (alive, dead)."""
        import os as _os
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, path FROM reader_refs WHERE alive=1"
        ).fetchall()
        dead_ids = [r["id"] for r in rows if not _os.path.exists(r["path"])]
        if dead_ids:
            conn.execute(
                f"UPDATE reader_refs SET alive=0 WHERE id IN ({','.join('?'*len(dead_ids))})",
                dead_ids,
            )
            conn.commit()
        return len(rows) - len(dead_ids), len(dead_ids)

    def delete_dead_refs(self, older_than_hours: float = 24.0):
        """Delete refs that have been dead for more than older_than_hours."""
        cutoff = time.time() - older_than_hours * 3600
        conn = self._conn()
        conn.execute(
            "DELETE FROM reader_refs WHERE alive=0 AND ts_last_seen < ?", (cutoff,)
        )
        conn.commit()

    def recent_reader_refs(self, minutes: float = 60.0, limit: int = 50,
                           alive_only: bool = True) -> list[dict]:
        """Return reader refs seen recently, newest first."""
        since = time.time() - minutes * 60
        if alive_only:
            rows = self._conn().execute(
                """SELECT * FROM reader_refs WHERE ts_last_seen >= ? AND alive=1
                   ORDER BY ts_last_seen DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
        else:
            rows = self._conn().execute(
                """SELECT * FROM reader_refs WHERE ts_last_seen >= ?
                   ORDER BY ts_last_seen DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_reader_refs(self, alive_only: bool = True) -> list[dict]:
        """Return all reader refs (for ping watcher)."""
        if alive_only:
            rows = self._conn().execute(
                "SELECT * FROM reader_refs WHERE alive=1 ORDER BY ts_last_seen DESC"
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM reader_refs ORDER BY ts_last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Prune ──────────────────────────────────────────────────────────────

    def prune(self, events_hours: float = 4.0,
              checkpoints_hours: float = 24.0,
              ocr_hours: float = 2.0):
        now = time.time()
        conn = self._conn()
        conn.execute("DELETE FROM events WHERE ts < ?", (now - events_hours * 3600,))
        conn.execute("DELETE FROM checkpoints WHERE ts < ?", (now - checkpoints_hours * 3600,))
        conn.execute("DELETE FROM ocr_snapshots WHERE ts < ?", (now - ocr_hours * 3600,))
        conn.execute("DELETE FROM vision_snapshots WHERE ts < ?", (now - ocr_hours * 3600,))
        conn.execute("DELETE FROM audio_transcripts WHERE ts < ?", (now - events_hours * 3600,))
        conn.commit()
        # Force WAL checkpoint so Docker can always read up-to-date data
        # without the WAL growing unboundedly
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def prune_by_size(self, max_bytes: int):
        """Delete oldest events/ocr/vision if DB exceeds max_bytes."""
        import os as _os
        try:
            db_size = _os.path.getsize(self._path)
            if db_size <= max_bytes:
                return
            conn = self._conn()
            # Delete oldest 10% of events
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            conn.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY ts ASC LIMIT ?)",
                (max(1, count // 10),)
            )
            conn.commit()
        except Exception:
            pass
