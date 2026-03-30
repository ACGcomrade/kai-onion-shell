"""
ContextPackager — token-efficient context for AI.

Format (~150-200 tokens):
  [CONTEXT 14:32 | 10min | 8 events]
  NOW: VS Code "store.py" | clip:142b | cmds: git diff, pytest
  SCREEN@14:31 (conf:0.82) [+42w -5w]: class SQLiteStore...
  TIMELINE:
  14:22 app Safari→VSCode "store.py"
  14:25 file 2× ~/projects/
  14:31 cmd pytest tests/
  [Q] {question}
"""
from __future__ import annotations
import difflib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import re as _re
from core.store import Store
from core.file_reader import gather_file_context


def _hm(ts: float) -> str:
    # astimezone() converts UTC→local, respects TZ env var set by Docker
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%H:%M")


def _screen_delta(older_ocr: str, newer_ocr: str) -> str:
    """Compute compact word-level diff: '+Nw -Mw: <new words preview>'"""
    a = older_ocr.split() if older_ocr else []
    b = newer_ocr.split() if newer_ocr else []
    if not a:
        return newer_ocr[:300]  # first OCR ever, show raw

    sm = difflib.SequenceMatcher(None, a, b)
    added = []
    removed_count = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(b[j1:j2])
        if tag in ("delete", "replace"):
            removed_count += i2 - i1

    if not added and removed_count == 0:
        return "(unchanged)"

    preview = " ".join(added[:30])
    delta = f"+{len(added)}w"
    if removed_count:
        delta += f" -{removed_count}w"
    if preview:
        delta += f": {preview}"
    return delta


import os as _os


def _fmt_fs_state(fs_data: dict, level: int) -> str:
    """
    Format filesystem snapshot into compact context lines.
    New format stores dirs/files/subdirs separately — always show ALL folders,
    truncate only files to keep token count reasonable.
    Also supports legacy format (items list) for backwards compatibility.
    """
    lines = []
    for d, info in fs_data.items():
        label = _os.path.basename(d) or d
        recent = set(info.get("recent", []))

        # ── New format: dirs + files + subdirs separate ────────────────────
        if "dirs" in info:
            dirs_list  = info.get("dirs", [])
            files_list = info.get("files", [])
            subdirs    = info.get("subdirs", {})
            total      = info.get("total", len(dirs_list) + len(files_list))

            def mark(name: str, suffix: str = "") -> str:
                return f"*{name}{suffix}" if name in recent else f"{name}{suffix}"

            # Always show every directory — folders are the key navigation info
            dir_tokens  = [mark(n, "/") for n in dirs_list]
            # Files: show up to 20 at level≥4, 10 otherwise
            file_limit  = 20 if level >= 4 else 10
            file_tokens = [mark(n) for n in files_list[:file_limit]]
            hidden_files = max(0, len(files_list) - file_limit)

            parts = dir_tokens + file_tokens
            line = f"  ~/{label} ({total}): " + "  ".join(parts)
            if hidden_files:
                line += f"  [+{hidden_files} files]"
            lines.append(line)

            # Show subdirectory contents (one level deep) — only non-empty subdirs
            if subdirs and level >= 2:
                for folder, children in subdirs.items():
                    if not children:
                        continue
                    child_tokens = []
                    for c in children:
                        cname = c.rstrip("/")
                        child_tokens.append(mark(cname, "/" if c.endswith("/") else ""))
                    # Cap per subdir to keep tokens reasonable
                    cap = 30 if level >= 4 else 15
                    shown_children = child_tokens[:cap]
                    rest = len(children) - len(shown_children)
                    sub_line = f"    ~/{label}/{folder}/: " + "  ".join(shown_children)
                    if rest:
                        sub_line += f"  [+{rest} more]"
                    lines.append(sub_line)

        # ── Legacy format: flat items list ─────────────────────────────────
        else:
            items = info.get("items", [])
            total = info.get("total", len(items))
            marked = [f"*{it}" if it.rstrip("/") in recent else it for it in items]
            show = 20 if level >= 4 else 12
            shown = marked[:show]
            rest  = total - len(shown)
            line  = f"  ~/{label} ({total}): " + "  ".join(shown)
            if rest:
                line += f"  [+{rest} more]"
            lines.append(line)

    return "\n".join(lines)


_CAT_ORDER = ["browsing", "coding", "files", "content", "navigation", "system", "other"]
_CAT_LABEL = {
    "browsing":   "BROWSING",
    "coding":     "CODING",
    "files":      "FILES",
    "content":    "CONTENT",
    "navigation": "NAVIGATION",
    "system":     "SYSTEM",
    "other":      "OTHER",
}


def _build_grouped_timeline(events: list[dict], level: int) -> str:
    """Group events by category, collapse low-importance system noise."""
    if not events:
        return "(no recent events)"

    # Bucket events by category
    buckets: dict[str, list[dict]] = {c: [] for c in _CAT_ORDER}
    for ev in events:
        cat = ev.get("category") or "other"
        if cat not in buckets:
            buckets[cat] = []
        buckets[cat].append(ev)

    lines = [f"TIMELINE [{len(events)} events]:"]
    for cat in _CAT_ORDER:
        evs = buckets.get(cat, [])
        if not evs:
            continue
        label = _CAT_LABEL.get(cat, cat.upper())

        # Collect domain summary for browsing
        if cat == "browsing":
            domains = []
            for e in evs:
                d = e.get("domain", "")
                if d and (not domains or domains[-1] != d):
                    domains.append(d)
            domain_str = "→".join(domains[:5])
            if domain_str:
                label += f" [{domain_str}]"

        # Collapse system/navigation low-importance events
        if cat in ("system", "navigation") and level <= 3:
            # Show max 3 lines, summarise rest
            shown = evs[-3:]
            rest = len(evs) - len(shown)
            lines.append(f"{label} ({len(evs)} events):" if rest else f"{label}:")
            for e in shown:
                lines.append(f"  {_hm(e['ts'])} {e['summary'][:80]}")
        else:
            lines.append(f"{label}:")
            # Cap per category to keep tokens reasonable
            cap = 15 if level >= 4 else 8
            shown = evs[-cap:]
            rest = len(evs) - len(shown)
            if rest:
                lines.append(f"  (... {rest} earlier events)")
            for e in shown:
                lines.append(f"  {_hm(e['ts'])} {e['channel'][:4]} {e['summary'][:80]}")

    return "\n".join(lines)


_MEDIA_PATH_RE = _re.compile(
    r"""
    (?:
        # Absolute path with media extension
        /[^\s"'<>，。！？\u3000-\u9fff]+\.(?:jpg|jpeg|png|gif|webp|bmp|tiff?|heic|heif|avif|ico
             |mp4|mov|avi|mkv|webm|m4v|flv|wmv|mp3|wav|m4a|flac|aac|ogg|wma)
        |
        # Relative path or filename with media extension
        [\w\-. /~]+\.(?:jpg|jpeg|png|gif|webp|bmp|tiff?|heic|heif|avif|ico
             |mp4|mov|avi|mkv|webm|m4v|flv|wmv|mp3|wav|m4a|flac|aac|ogg|wma)
    )
    """,
    _re.VERBOSE | _re.IGNORECASE,
)


def build_context(question: str, store: Store,
                  window_minutes: float = 10.0, level: int = 3,
                  conf=None) -> str:
    # conf kept for backward compatibility but no longer used for media
    now = time.time()
    since_ts = now - window_minutes * 60

    # ── Current state ─────────────────────────────────────────────────────
    cp = store.latest_checkpoint()
    app_name = cp["app_name"] if cp else "?"
    window_title = cp["window_title"] if cp else ""
    recent_cmds = cp["recent_commands"][-3:] if cp else []
    clip_hash = cp["clipboard_hash"] if cp else ""

    now_str = datetime.now(tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    # Find most recent clipboard content from event log (more useful than hash alone).
    # The clipboard event summary has format: "Clipboard changed (N chars): PREVIEW"
    latest_clip_preview = ""
    for e in reversed(store.recent_events(minutes=30, limit=200)):
        if e["channel"] == "clipboard":
            summary = e.get("summary", "")
            if ": " in summary:
                latest_clip_preview = summary.split(": ", 1)[1][:100]
            break

    # NOW line: the ground truth for what is currently on screen.
    # Window title and URL are the most reliable real-time signals.
    # AI MUST prioritize these over any vision description (which has up to 30s lag).
    now_lines = [f"NOW (ground truth — trust these over all vision/history below):"]
    now_lines.append(f"  app={app_name}")
    if window_title:
        now_lines.append(f'  window_title="{window_title}"')
    if cp and cp.get("url"):
        now_lines.append(f'  url={cp["url"][:120]}')
    if latest_clip_preview:
        now_lines.append(f'  clipboard_content="{latest_clip_preview}"')
    if recent_cmds:
        now_lines.append("  recent_cmds=" + "; ".join(recent_cmds))
    now_line = "\n".join(now_lines)

    # ── Filesystem state (current, not just recent) ────────────────────────
    fs_section = ""
    if level >= 2:
        fs_snap = store.latest_fs_state()
        if fs_snap:
            age_min = (now - fs_snap["ts"]) / 60
            fs_section = f"FILESYSTEM (as of {int(age_min)}min ago):\n"
            fs_section += _fmt_fs_state(fs_snap["data"], level)

    # ── Vision section (semantic screen description) ──────────────────────
    # NOTE FOR AI: Vision descriptions have 15-30s inference lag. They describe
    # what was on screen WHEN the screenshot was taken, which may differ from NOW.
    # Always use window_title/url/clipboard_content above as the authoritative source.
    # Vision is useful for understanding *content* (e.g. what a video showed visually),
    # NOT for determining *which* app/page/video is currently active.
    vision_section = ""
    if level >= 2:
        # Check for fresh user_ask HQ vision (most prominent — shown at top)
        ask_vision = store.latest_user_ask_vision(max_age_seconds=30)
        if ask_vision:
            vision_section = (
                f"SCREEN_NOW@{_hm(ask_vision['ts'])} [HIGH-QUALITY CAPTURE]: "
                f"{ask_vision['description']}"
            )

        # Camera vision (physical presence) — separate from screen vision
        camera_visions = store.recent_vision(minutes=window_minutes + 5, limit=3, source="camera")
        if camera_visions:
            latest_cam = camera_visions[-1]
            cam_age = int((now - latest_cam["ts"]) / 60)
            cam_age_str = f"{cam_age}min ago" if cam_age > 0 else "just now"
            vision_section_parts = [vision_section] if vision_section else []
            vision_section_parts.append(
                f"CAMERA@{_hm(latest_cam['ts'])} ({cam_age_str}): {latest_cam['description']}"
            )
            vision_section = "\n".join(vision_section_parts)

        # Background vision sequence (chronological, for context history)
        # IMPORTANT: vision inference takes 15-30s, so each entry describes
        # the screen as it was at that timestamp, not necessarily right now.
        vision_limit = 15 if level >= 4 else 10
        visions = store.recent_vision(minutes=window_minutes + 5, limit=vision_limit, source="screen")
        # Exclude user_ask from sequence (already shown above)
        bg_visions = [v for v in visions if v.get("trigger") != "user_ask"]
        if bg_visions:
            trail = []
            for v in bg_visions:
                age_v = int((now - v["ts"]) / 60)
                stale = " [MAY BE STALE — captured before window change]" if age_v > 1 else ""
                trail.append(
                    f"  VISION@{_hm(v['ts'])} ({age_v}min ago){stale} "
                    f"[{v.get('window_title','')[:50]}]: "
                    f"{v['description'][:120]}"
                )
            bg_section = "SCREEN_VISION_HISTORY (oldest→newest):\n" + "\n".join(trail)
            # Only show history if it doesn't conflict with the current window title
            # (i.e., the latest bg vision window matches current window)
            latest_bg = bg_visions[-1]
            latest_bg_age = int((now - latest_bg["ts"]) / 60)
            if latest_bg_age <= 1:
                # Recent enough — show prominently
                bg_section = (
                    f"SCREEN_VISION@{_hm(latest_bg['ts'])} "
                    f"[{latest_bg.get('window_title','')[:60]}]: "
                    f"{latest_bg['description']}"
                )
                if len(bg_visions) > 1:
                    bg_section += "\n" + "\n".join(trail[:-1])
            if vision_section:
                vision_section += "\n" + bg_section
            else:
                vision_section = bg_section

    # ── Screen section ────────────────────────────────────────────────────
    # OCR text is the most reliable real-time signal — captured synchronously at ask time.
    # It always reflects what was on screen at the moment, unlike vision (which has
    # 15-30s inference lag). Show full OCR text for the latest snapshot.
    screen_line = ""
    if level >= 3:
        ocr_pair = store.two_latest_ocr()
        if ocr_pair:
            latest_ocr = ocr_pair[-1]
            if latest_ocr["ts"] >= since_ts:
                age_ocr = int((now - latest_ocr["ts"]) / 60)
                age_str = f"{age_ocr}min ago" if age_ocr > 0 else "just now"
                # Always show enough OCR text to identify content (not just delta)
                words = latest_ocr["ocr_text"].split()
                word_limit = 200 if level >= 4 else 120
                preview = " ".join(words[:word_limit])
                conf = latest_ocr["confidence"]
                screen_line = (
                    f"SCREEN_OCR@{_hm(latest_ocr['ts'])} ({age_str}, conf:{conf:.2f}) "
                    f"[this is what was literally on screen — trust over vision descriptions]:\n"
                    f"{preview}"
                )
                # If there's a second OCR, show the diff too
                if len(ocr_pair) == 2:
                    older, newer = ocr_pair
                    if older["ts"] >= since_ts:
                        delta = _screen_delta(older["ocr_text"], newer["ocr_text"])
                        screen_line += f"\n  (change vs {_hm(older['ts'])}: {delta})"

    # ── Events timeline (grouped by category) ────────────────────────────
    events = store.recent_events(minutes=window_minutes, limit=100)
    # Drop low-importance noise at level ≤ 3
    if level <= 3:
        events = [e for e in events if e.get("importance", 2) > 0]

    timeline = _build_grouped_timeline(events, level)

    # ── File content (read-only, on-demand) ──────────────────────────────
    file_parts = []
    if level >= 2:
        file_hits = gather_file_context(
            question=question,
            window_title=window_title,
            app_name=app_name,
            max_files=2 if level <= 3 else 3,
        )
        for hit in file_hits:
            source_tag = "currently open" if hit["source"] == "editor" else "referenced"
            lang = hit["path"].suffix.lstrip('.') or "text"
            header_line = f"FILE ({source_tag}): {hit['path']}"
            file_parts.append(f"{header_line}\n```{lang}\n{hit['content']}\n```")

    # ── Reader files (local file references, path-linked, no copy) ────────
    reader_parts = []
    if level >= 2:
        # Show files seen in the context window + any explicitly mentioned in question
        reader_refs = store.recent_reader_refs(minutes=window_minutes, limit=20)

        # Also search for explicit file mentions in the question
        question_lower = question.lower()
        all_refs = store.all_reader_refs(alive_only=True) if question_lower else []
        for ref in all_refs:
            path_obj = _os.path.basename(ref["path"])
            if path_obj.lower() in question_lower or ref["path"].lower() in question_lower:
                if not any(r["path"] == ref["path"] for r in reader_refs):
                    reader_refs.append(ref)

        for ref in reader_refs[:15]:
            path_short = ref["path"].replace(str(Path.home()), "~")
            ftype = ref.get("file_type", "file").upper()
            alive_tag = "" if ref.get("alive", 1) else " [DELETED]"
            summary = ref.get("summary") or ref.get("description", "")
            summary = summary[:120] if summary else ""
            line = f"  {ftype}: {path_short}{alive_tag}"
            if summary:
                line += f" — {summary}"
            reader_parts.append(line)

    # ── Media file descriptions (pre-analyzed by watcher, read from DB) ──
    media_parts = []
    if level >= 2:
        # Recently analyzed files
        recent_media = store.recent_file_descriptions(
            minutes=window_minutes * 2, limit=10
        )
        # Also check for explicit media path references in the question
        extra_paths = set()
        for m in _MEDIA_PATH_RE.finditer(question):
            ref = m.group(0).strip()
            p = str(_os.path.expanduser(ref))
            extra_paths.add(p)
        for mp in extra_paths:
            hit = store.get_file_description(mp)
            if hit:
                # Don't duplicate if already in recent_media
                if not any(r["path"] == mp for r in recent_media):
                    recent_media.append(hit)

        for fd in recent_media:
            ftype = fd["file_type"].upper()
            path_short = fd["path"].replace(str(Path.home()), "~")
            dur_str = ""
            if fd.get("duration_s") and fd["duration_s"] > 0:
                mins = int(fd["duration_s"] // 60)
                secs = int(fd["duration_s"] % 60)
                dur_str = f" ({mins}:{secs:02d})"
            desc = fd.get("summary") or fd.get("description", "")[:150]
            audio_str = ""
            if fd.get("transcript"):
                audio_str = f' Audio: "{fd["transcript"][:80]}"'
            media_parts.append(
                f"  {ftype}: {path_short}{dur_str} — {desc}{audio_str}"
            )

    # ── Audio transcripts ─────────────────────────────────────────────────
    audio_section = ""
    if level >= 2:
        audio_recs = store.recent_audio(minutes=window_minutes, limit=20)
        if audio_recs:
            lines = []
            for a in audio_recs:
                dur = f"{a['duration_s']:.0f}s" if a.get("duration_s") else ""
                lang = f" [{a['language']}]" if a.get("language") else ""
                lines.append(f"  {_hm(a['ts'])} voice{lang} ({dur}): {a['transcript'][:120]}")
            audio_section = "VOICE TRANSCRIPTS:\n" + "\n".join(lines)

    # ── Assemble ──────────────────────────────────────────────────────────
    header = f"[CONTEXT {now_str} | {int(window_minutes)}min | {len(events)} events]"
    parts = [header, now_line]
    if fs_section:
        parts.append(fs_section)
    if vision_section:
        parts.append(vision_section)
    if screen_line:
        parts.append(screen_line)
    if audio_section:
        parts.append(audio_section)
    parts.append("TIMELINE:")
    parts.append(timeline)
    if reader_parts:
        parts.append("READER_FILES (local files on disk — path references):")
        parts.extend(reader_parts)
    if media_parts:
        parts.append("MEDIA FILES (analyzed content):")
        parts.extend(media_parts)
    if file_parts:
        parts.append("FILES:")
        parts.extend(file_parts)
    parts.append(f"[Q] {question}")

    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
