"""
core/bomber.py — Breadth-first temporal downsampling for kaiData.

Strategy: divide time into buckets; keep one representative entry per bucket.
Older buckets are coarser (wider). This maximises temporal coverage while
freeing disk space.

Base temporal pyramid (bucket sizes scale linearly with bomb_score):
  0  –   1h : keep all  (always preserved)
  1h –  24h : 1 per 5 min bucket  (base)
  24h –   7d : 1 per hour bucket  (base)
  7d –  30d : 1 per day bucket   (base)
  >30d       : 1 per week bucket  (base)

Bombing is driven by bomb_score ∈ [0, 1]:
  bomb_score = α·ratio + β·(1−similarity) + γ·time_pressure
  α=0.40, β=0.30, γ=0.30

  Two linear axes respond to bomb_score:
    min_age_s   : minimum data age to touch, lerp 30d→1h as score 0.4→1.0
    bucket_scale: bucket size multiplier,    lerp 1.0×→3.0× as score 0.4→1.0

  High bomb_score → fresher data gets touched, with coarser buckets.
  Low bomb_score  → only old data (>30d) is thinned at default granularity.

Storage override: if total_disk > max_bytes, runs with bomb_score=1.0 and
repeats until size is within budget (hard loop, uncapped).

Applied to: events, ocr_snapshots, vision_snapshots, audio_transcripts.
NOT applied to: reader_refs, checkpoints, file_descriptions.
Screenshots are deleted when their parent ocr_snapshot is deleted.
"""
from __future__ import annotations
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.store import Store

log = logging.getLogger("onion.bomber")

# ── Pyramid (base bucket sizes — scaled at runtime) ───────────────────────────
# (age_min_s, age_max_s, base_bucket_s)
# None for age_max_s = "no upper bound"; None for base_bucket_s = "keep all"
PYRAMID: list[tuple[int, int | None, int | None]] = [
    (0,          3_600,      None),        # 0–1h: keep all
    (3_600,      86_400,     300),          # 1h–24h: 1 per 5min
    (86_400,     604_800,    3_600),        # 24h–7d: 1 per hour
    (604_800,    2_592_000,  86_400),       # 7d–30d: 1 per day
    (2_592_000,  None,       604_800),      # >30d: 1 per week
]

TABLES = ["events", "ocr_snapshots", "vision_snapshots", "audio_transcripts"]

# ── Bomb score constants ──────────────────────────────────────────────────────
BOMB_THRESHOLD   = 0.40   # minimum bomb_score to trigger maintenance run
BOMB_ALPHA       = 0.40   # weight: data change ratio
BOMB_BETA        = 0.30   # weight: redundancy = 1 − similarity
BOMB_GAMMA       = 0.30   # weight: time pressure
BOMB_T_DAYS      = 3.0    # time decay constant (days)

# Linear axes boundaries
MIN_AGE_AT_MAX_SCORE = 3_600        # 1 h  — data touched when score=1.0
MAX_AGE_AT_MIN_SCORE = 2_592_000    # 30 d — data touched when score≈threshold
MIN_BUCKET_SCALE     = 1.0          # default granularity at score=threshold
MAX_BUCKET_SCALE     = 3.0          # coarsest granularity at score=1.0

# Low-value OCR prune only kicks in when coverage > this value
# (avoids touching recent frames on routine low-score maintenance)
PRUNE_LOWVALUE_MIN_COVERAGE = 0.30


def compute_bomb_score(ratio: float, similarity: float,
                       ts_last_run: float,
                       T_days: float = BOMB_T_DAYS) -> float:
    """
    bomb_score = α·ratio + β·(1−S) + γ·f(Δt)

    ratio:       change_ratio["combined"]["ratio"] from KaiDataReader
    similarity:  data similarity S ∈ [0, 1] (see _data_similarity)
    ts_last_run: unix timestamp of last bomber run
    Returns score clamped to [0, 1].
    """
    delta_days = max(0.0, (time.time() - ts_last_run) / 86400.0)
    time_pressure = 1.0 - math.exp(-delta_days / T_days)
    redundancy = 1.0 - max(0.0, min(1.0, similarity))
    score = BOMB_ALPHA * ratio + BOMB_BETA * redundancy + BOMB_GAMMA * time_pressure
    return min(1.0, max(0.0, score))


def _data_similarity(store: "Store", ts_last_run: float,
                     n_samples: int = 200) -> float:
    """
    Measure how similar recent data (since ts_last_run) is to historical data
    using Normalized Compression Distance (NCD) over event summaries.

    NCD(x, y) = (C(x+y) − min(C(x), C(y))) / max(C(x), C(y))
    S = 1 − NCD  →  S ∈ [0, 1] where 1 = identical, 0 = completely different.

    High S → high redundancy → bomb more aggressively.
    Falls back to 0.5 (neutral) if there is insufficient text (<200 bytes).
    """
    import zlib

    conn = store._conn()
    now = time.time()
    window = now - ts_last_run

    # Fetch up to n_samples summaries from recent window and same-sized older window
    try:
        new_rows = conn.execute(
            "SELECT summary FROM events WHERE ts >= ? AND summary IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (ts_last_run, n_samples),
        ).fetchall()
        old_rows = conn.execute(
            "SELECT summary FROM events WHERE ts < ? AND ts >= ? AND summary IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (ts_last_run, ts_last_run - window, n_samples),
        ).fetchall()
    except Exception:
        return 0.5

    new_text = " ".join(r["summary"] for r in new_rows).encode()
    old_text = " ".join(r["summary"] for r in old_rows).encode()

    # Need at least 200 bytes each to get a meaningful NCD reading
    if len(new_text) < 200 or len(old_text) < 200:
        return 0.5

    cx = len(zlib.compress(new_text))
    cy = len(zlib.compress(old_text))
    cxy = len(zlib.compress(new_text + old_text))
    denom = max(cx, cy)
    if denom == 0:
        return 0.5
    ncd = (cxy - min(cx, cy)) / denom
    return max(0.0, min(1.0, 1.0 - ncd))


def _coverage_from_score(bomb_score: float) -> float:
    """Map bomb_score → coverage ∈ [0, 1] (0 at threshold, 1 at max)."""
    return max(0.0, (bomb_score - BOMB_THRESHOLD) / (1.0 - BOMB_THRESHOLD))


def _linear_params(coverage: float) -> tuple[float, float]:
    """Return (min_age_s, bucket_scale) for this coverage level."""
    min_age_s = (MAX_AGE_AT_MIN_SCORE
                 + coverage * (MIN_AGE_AT_MAX_SCORE - MAX_AGE_AT_MIN_SCORE))
    bucket_scale = MIN_BUCKET_SCALE + coverage * (MAX_BUCKET_SCALE - MIN_BUCKET_SCALE)
    return min_age_s, bucket_scale


def _collect_screenshot_paths(conn, ids: list[int]) -> list[str]:
    if not ids:
        return []
    try:
        rows = conn.execute(
            f"SELECT screenshot_path FROM ocr_snapshots WHERE id IN ({','.join('?'*len(ids))})",
            ids,
        ).fetchall()
        return [r["screenshot_path"] for r in rows if r["screenshot_path"]]
    except Exception:
        return []


def _delete_rows(conn, table: str, delete_ids: list[int]) -> list[str]:
    screenshot_paths: list[str] = []
    if table == "ocr_snapshots":
        screenshot_paths = _collect_screenshot_paths(conn, delete_ids)
    chunk = 500
    for i in range(0, len(delete_ids), chunk):
        batch = delete_ids[i:i+chunk]
        conn.execute(
            f"DELETE FROM {table} WHERE id IN ({','.join('?'*len(batch))})", batch
        )
    conn.commit()
    return screenshot_paths


def _prune_lowvalue(store: "Store", now: float, bucket_scale: float) -> int:
    """
    Pre-pass for ocr_snapshots: downsample low-value records.
    bucket_scale stretches bucket sizes linearly with bomb_score.

    Three tiers (priority order):
      is_away = 1          → 1 per (day × scale)
      confidence < 0.25    → 1 per (hour × scale)
      screen_change_score < 0.05 AND is_away = 0 → 1 per (5min × scale)
    """
    conn = store._conn()
    total_deleted = 0
    all_screenshot_paths: list[str] = []

    TIERS = [
        ("is_away = 1",                                       int(86_400 * bucket_scale), "away"),
        ("confidence < 0.25 AND is_away = 0",                 int(3_600 * bucket_scale),  "low_conf"),
        ("screen_change_score < 0.05 AND is_away = 0",        int(300 * bucket_scale),    "static"),
    ]

    handled_ids: set[int] = set()

    for where_clause, bucket_s, tier_name in TIERS:
        try:
            rows = conn.execute(
                f"SELECT id, ts FROM ocr_snapshots WHERE {where_clause} ORDER BY ts ASC"
            ).fetchall()
        except Exception:
            continue

        rows = [r for r in rows if r["id"] not in handled_ids]
        if not rows:
            continue

        kept: set[int] = set()
        bucket_seen: set[int] = set()
        for row in rows:
            bkey = int((now - row["ts"]) // bucket_s)
            if bkey not in bucket_seen:
                bucket_seen.add(bkey)
                kept.add(row["id"])

        all_ids = {r["id"] for r in rows}
        delete_ids = sorted(all_ids - kept)
        handled_ids.update(all_ids)

        if not delete_ids:
            continue

        sp = _delete_rows(conn, "ocr_snapshots", delete_ids)
        all_screenshot_paths.extend(sp)
        total_deleted += len(delete_ids)
        log.info(f"  ocr_snapshots [{tier_name}]: deleted {len(delete_ids)} rows")

    for sp in all_screenshot_paths:
        try:
            p = Path(sp)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    return total_deleted


def _downsample_table(store: "Store", table: str, now: float,
                      min_age_s: float, bucket_scale: float) -> int:
    """
    Downsample one table.  Only touches rows older than min_age_s.
    Each tier's bucket size is multiplied by bucket_scale.
    Returns number of rows deleted.
    """
    conn = store._conn()
    total_deleted = 0

    for age_min, age_max, base_bucket_s in PYRAMID:
        if base_bucket_s is None:
            continue  # "keep all" tier
        if age_min < min_age_s:
            continue  # below minimum age threshold for this bomb_score

        bucket_s = int(base_bucket_s * bucket_scale)
        ts_max = now - age_min
        ts_min = now - age_max if age_max is not None else 0.0

        rows = conn.execute(
            f"SELECT id, ts FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
            (ts_min, ts_max),
        ).fetchall()

        if not rows:
            continue

        kept: set[int] = set()
        bucket_seen: set[tuple] = set()
        for row in rows:
            bucket_idx = int((now - row["ts"]) // bucket_s)
            bkey = (bucket_s, bucket_idx)
            if bkey not in bucket_seen:
                bucket_seen.add(bkey)
                kept.add(row["id"])

        all_ids = {row["id"] for row in rows}
        delete_ids = sorted(all_ids - kept)

        if not delete_ids:
            continue

        screenshot_paths = _delete_rows(conn, table, delete_ids)
        total_deleted += len(delete_ids)

        for sp in screenshot_paths:
            try:
                p = Path(sp)
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    return total_deleted


def run_bomber(store: "Store", kaidata_dir: Path, max_bytes: int,
               bomb_score: float = 1.0) -> dict:
    """
    Run the bomber with the given bomb_score.

    bomb_score controls two linear axes:
      min_age_s:    lerp(30d → 1h) — how fresh data can be touched
      bucket_scale: lerp(1.0 → 3.0) — how coarse buckets become

    For storage override (size > max_bytes), caller passes bomb_score=1.0
    and repeats until size is within budget.

    Returns a summary dict.
    """
    from core.kaidata import total_disk_usage

    now = time.time()
    current_bytes = total_disk_usage(str(kaidata_dir))

    coverage = _coverage_from_score(bomb_score)
    min_age_s, bucket_scale = _linear_params(coverage)

    summary = {
        "ran": False,
        "bomb_score": bomb_score,
        "coverage": coverage,
        "min_age_s": min_age_s,
        "bucket_scale": bucket_scale,
        "bytes_before": current_bytes,
        "bytes_after": current_bytes,
        "rows_deleted": {},
    }

    log.info(
        f"Bomber running (score={bomb_score:.2f}, coverage={coverage:.1%}, "
        f"min_age={min_age_s/3600:.1f}h, bucket_scale={bucket_scale:.2f}x, "
        f"kaiData={current_bytes/1024**2:.1f} MB, budget={max_bytes/1024**3:.1f} GB)"
    )

    total_deleted = 0

    # Low-value OCR pre-pass (only when coverage is meaningful)
    if coverage >= PRUNE_LOWVALUE_MIN_COVERAGE:
        lv_deleted = _prune_lowvalue(store, now, bucket_scale)
        if lv_deleted:
            summary["rows_deleted"]["ocr_snapshots_lowvalue"] = lv_deleted
            total_deleted += lv_deleted

    for table in TABLES:
        deleted = _downsample_table(store, table, now, min_age_s, bucket_scale)
        if deleted:
            summary["rows_deleted"][table] = deleted
            total_deleted += deleted
            log.info(f"  {table}: deleted {deleted} rows")

    if total_deleted > 0:
        try:
            store._conn().execute("PRAGMA wal_checkpoint(PASSIVE)")
            store._conn().execute("VACUUM")
            store._conn().commit()
        except Exception as e:
            log.warning(f"VACUUM failed: {e}")

    summary["ran"] = True
    summary["bytes_after"] = total_disk_usage(str(kaidata_dir))
    freed = summary["bytes_before"] - summary["bytes_after"]
    log.info(f"Bomber done: deleted {total_deleted} rows, freed {freed/1024**2:.1f} MB")
    return summary


def should_run_bomber(ratio: float, similarity: float,
                      ts_last_run: float, max_bytes: int,
                      kaidata_dir: Path) -> tuple[bool, float]:
    """
    Compute bomb_score and decide whether to run.

    Returns (should_run: bool, bomb_score: float).
    Storage overflow always returns (True, 1.0).
    Maintenance returns (True, score) when score > BOMB_THRESHOLD.
    """
    from core.kaidata import total_disk_usage
    if total_disk_usage(str(kaidata_dir)) > max_bytes:
        return True, 1.0
    score = compute_bomb_score(ratio, similarity, ts_last_run)
    return score > BOMB_THRESHOLD, score
