# Onion Shell

A local background watcher + packager that gives any LLM complete awareness of your computer activity.

The premise: a language model answering "what was I just working on?" or "describe that image on my desktop" should not need you to explain your current state. Onion Shell assembles that context automatically — from apps, screens, files, clipboard, terminal, and media files — and injects it silently at query time.

---

## Architecture

Onion Shell runs as two independent processes sharing a single SQLite database:

```
┌──────────────────────────────────────┐     ┌──────────────────────────────┐
│  Watcher (Python on host)            │     │  Web UI (Docker / FastAPI)   │
│  - macOS permissions required        │     │  - No host permissions        │
│  - Screen OCR + vision LLM           │─────│  - Reads from shared SQLite   │
│  - Keyboard + mouse tracking         │     │  - Sends AI queries           │
│  - Media file analysis               │     │  - Streams answers via SSE    │
│  - Audio + camera (opt-in)           │     │                              │
│  - Writes ALL data to SQLite         │     └──────────────────────────────┘
└──────────────────────────────────────┘
                    │
           ~/.onion_shell/db.sqlite
```

**Why the split?** Docker cannot access your Desktop, Downloads, or screen. All file reading, screen capture, and media analysis happens on the host watcher. The web UI runs in Docker (or bare Python) because it only needs to read the database and make AI API calls.

---

## What it captures

| Channel | Method | Frequency (level 3) | Storage |
|---|---|---|---|
| Active app + window title | AppleScript (macOS) | every 3s | `events` table |
| Browser URL | AppleScript | every 3s (browser only) | `events` + `checkpoints` |
| Clipboard content | pyperclip | every 5s | `events` (80-char preview) |
| Terminal commands | shell history | every 10s | `events` |
| File system changes | watchdog | real-time | `events` |
| Screen OCR | mss + ocrmac/tesseract | on title change + 60s fallback | `ocr_snapshots` (zlib compressed) |
| Screen vision | Ollama vision LLM | every 30s (browser/video apps) | `vision_snapshots` |
| Image files | Pillow + vision LLM | on new file + on-demand | `file_descriptions` |
| Video files | cv2 keyframes + ffmpeg + whisper | on new file + on-demand | `file_descriptions` |
| Audio files | faster-whisper | on new file + on-demand | `file_descriptions` |
| Microphone (opt-in) | sounddevice + VAD + faster-whisper | speech-triggered | `audio_transcripts` |
| Camera (opt-in) | opencv + vision LLM | every 120s | `vision_snapshots` (source=camera) |
| Keyboard typing | pynput | 3s silence flush | `events` |
| Mouse clicks | pynput | per click | `events` (screen zone only, not pixel) |

Polling intervals automatically scale up to 10x when the system is idle (HID idle > 120s).

---

## Media analysis pipeline

All media analysis runs on the host watcher. Results are stored in the database and included in AI context at query time — Docker never touches your files directly.

**Images** (jpg, jpeg, png, gif, webp, bmp, tiff, heic, heif, avif, ico):
1. Watchdog detects new file, or on-demand via `~/.onion_shell/describe_media_trigger`
2. Pillow loads and resizes to ≤1280px on the longest edge (falls back to raw bytes for JPEG/PNG if Pillow is unavailable)
3. Frame encoded as base64 JPEG
4. Sent to vision LLM: Claude API → Ollama HQ model → moondream fallback
5. Description + summary saved to `file_descriptions` table
6. Packager reads from DB and includes results in `MEDIA FILES:` section at query time

**Videos** (mp4, mov, avi, mkv, webm, m4v, flv, wmv):
1. cv2 extracts 5 evenly-spaced keyframes
2. Each frame resized to ≤1280px and described by vision LLM with its timestamp
3. ffmpeg extracts audio track → faster-whisper transcribes
4. Combined keyframe descriptions + transcript saved to `file_descriptions`

**Audio files** (mp3, wav, m4a, flac, aac, ogg, wma):
1. faster-whisper transcribes directly from the file path
2. Transcript + duration saved to `file_descriptions`

Files are re-analyzed if the stored description is more than 1 hour old. Entries for deleted files older than 7 days are pruned automatically.

---

## Configuration

Config file: `~/.onion_shell/config.toml` (created with defaults on first run).

```toml
[monitoring]
level = 3           # 1=minimal  2=basic  3=standard  4=detailed  5=maximum
privacy = "standard"  # "strict" | "standard" | "minimal"
storage_gb = 2.0    # max GB for Onion data — controls long-term memory budget
                    # when DB exceeds this, oldest 10% of events are deleted

[providers]
# AI provider priority — tries each in order, skips unconfigured ones
priority = "ollama,openai,claude,gemini"
ollama_url = "http://localhost:11434"
ollama_model = "deepseek-r1:latest"
ollama_vision_model = "moondream"        # lightweight background vision model
ollama_vision_model_hq = ""              # HQ vision at ask time (e.g. "qwen2.5-vl:7b")
openai_api_key = ""
openai_model = "gpt-4o-mini"
anthropic_api_key = ""
anthropic_model = "claude-haiku-4-5-20251001"
google_api_key = ""
google_model = "gemini/gemini-1.5-flash"

[retention]
events_hours = 4       # rolling window for events table
checkpoints_hours = 24
ocr_hours = 2

[sensors]
audio_enabled = false     # opt-in: microphone transcription
audio_language = ""       # "" = auto-detect; "en" / "zh" / etc.
audio_model_size = "base" # faster-whisper size: tiny / base / small / medium
camera_enabled = false    # opt-in: webcam capture
camera_interval = 120     # seconds between camera snapshots
input_enabled = true      # keyboard + mouse tracking
```

**Storage budget**: `storage_gb = 2.0` is the master knob for long-term memory. The DB is pruned hourly. Increase it (e.g. `storage_gb = 10.0`) for longer history; decrease it on disk-constrained or privacy-sensitive systems.

**Monitoring level** controls polling granularity:

| Level | App poll | OCR trigger | Vision interval | Clipboard poll |
|---|---|---|---|---|
| 1 | 30s | never | — | 30s |
| 2 | 10s | app switch | 120s | 10s |
| 3 | 3s | title change | 30s | 5s |
| 4 | 2s | title change | 15s | 3s |
| 5 | 1s | title change | 10s | 2s |

---

## SQLite database schema

All data lives at `~/.onion_shell/db.sqlite` (WAL mode, shared between watcher and web UI).

| Table | Contents |
|---|---|
| `events` | Every captured change: app switch, URL, clipboard, terminal command, file event, keyboard segment, mouse click |
| `checkpoints` | Periodic full-state snapshot: app + window title + URL + clipboard hash + recent commands |
| `ocr_snapshots` | Screen text (zlib-compressed), triggered by title change or periodic fallback |
| `vision_snapshots` | Vision LLM descriptions of screen (source=screen) and webcam (source=camera) |
| `audio_transcripts` | Microphone speech segments from VAD-triggered transcription |
| `file_descriptions` | Analyzed media files: image descriptions, video keyframe summaries + audio transcripts |
| `fs_snapshots` | Directory listings of Desktop / Downloads / Documents / Pictures |

---

## Privacy

- **Everything is local.** No telemetry, no cloud sync. Data only leaves the machine when you submit a question (sent to your configured AI provider).
- **Blocked apps**: password managers (1Password, Bitwarden, Keychain Access, etc.) pause ALL capture while they are frontmost.
- **Privacy mode**: `"strict"` pauses capture when any privacy-category app is active; `"standard"` keeps most content but suppresses keyboard capture in sensitive apps.
- **Raw media is never stored** — only text descriptions.
- **`onion pause`** stops all capture immediately.
- Sensitive patterns (API keys, passwords, credit cards) are redacted from events in standard mode.

---

## Setup

```bash
cd "kai onion shell"
./onion setup      # create local venv + install dependencies
./onion start      # start watcher + web UI
# Open http://localhost:7070
```

Optional dependencies (install as needed):

```bash
pip install faster-whisper webrtcvad sounddevice  # microphone transcription
pip install opencv-python-headless                 # webcam + video keyframe extraction
pip install Pillow                                 # image loading (HEIC, resize, all formats)
pip install pynput                                 # keyboard + mouse tracking
brew install ffmpeg                                # audio extraction from video files
```

On macOS, `pynput` also requires Accessibility permission: System Settings → Privacy & Security → Accessibility.

---

## Usage

Ask anything in the web UI at `http://localhost:7070`. Examples:

- "我刚才在看什么？" (what was I just watching?)
- "桌面上那张截图里是什么？" (what's in that screenshot on my desktop?)
- "帮我总结一下刚才看的视频" (summarize the video I just watched)
- "我过去10分钟做了什么？" (what did I do in the last 10 minutes?)

**On-demand media analysis** — trigger analysis of a specific file without waiting for the background scan:

```bash
echo -n "/path/to/file.jpg" > ~/.onion_shell/describe_media_trigger
# Watcher picks it up within ~15s and stores the result in file_descriptions
```

---

## Commands

```bash
./onion start      # start watcher + web UI
./onion stop       # stop everything
./onion restart    # restart (kills old processes cleanly)
./onion pause      # pause all capture
./onion resume     # resume capture
./onion status     # show current state
./onion setup      # create venv + install deps
./onion install    # setup + add onion to PATH
```

---

## Data layout

```
~/.onion_shell/
  config.toml              — user configuration
  db.sqlite                — all captured data (shared between host + Docker)
  watcher.log              — watcher daemon logs
  ask_trigger              — flag file: web UI signals watcher to capture immediately
  reinit                   — flag file: triggers a fresh filesystem snapshot
  describe_media_trigger   — flag file: path of media file for immediate analysis
  cpu_status               — current CPU% written by daemon
  interval                 — custom polling interval override
```
