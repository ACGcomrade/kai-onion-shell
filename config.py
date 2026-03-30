"""
Onion Shell — Configuration

Settings are read from:
  1. ~/.onion_shell/config.toml  (user config, created on first run)
  2. Environment variables       (override everything)
  3. Defaults below
"""
from __future__ import annotations
import os
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib   # pip install tomli (for Python < 3.11)
    except ImportError:
        tomllib = None            # fallback: use defaults
from pathlib import Path
from dataclasses import dataclass, field


DATA_DIR = Path.home() / ".onion_shell"
CONFIG_FILE = DATA_DIR / "config.toml"
DB_PATH = DATA_DIR / "db.sqlite"   # legacy path — new path is in kaidata.py

# Default kaiData location (next to the project on the same volume)
_PROJECT_DIR = Path(__file__).parent
KAIDATA_DIR_DEFAULT = _PROJECT_DIR.parent / "kaiData"

DEFAULT_CONFIG = """\
[monitoring]
level = 3           # 1=minimal  2=basic  3=standard  4=detailed  5=maximum
privacy = "standard"  # "strict" | "standard" | "minimal"
storage_gb = 2.0    # max GB for kaiData (long-term memory budget, up to 200)
# kaidata_path = ""  # override kaiData location; empty = auto (next to project)

[providers]
# AI provider priority — tries each in order, skips unconfigured ones
priority = "ollama,openai,claude,gemini"
ollama_url = "http://localhost:11434"
ollama_model = "deepseek-r1:latest"
ollama_vision_model = "moondream"        # lightweight background vision model
ollama_vision_model_hq = ""              # high-quality vision model at ask time (e.g. "qwen2.5-vl:7b"), empty=fallback to moondream
openai_api_key = ""
openai_model = "gpt-4o-mini"
anthropic_api_key = ""
anthropic_model = "claude-haiku-4-5-20251001"
google_api_key = ""
google_model = "gemini/gemini-1.5-flash"

[retention]
events_hours = 4       # how long to keep event log
checkpoints_hours = 24
ocr_hours = 2

[sensors]
# Microphone transcription — requires: pip install sounddevice faster-whisper webrtcvad
audio_enabled = false     # set true to enable (opt-in)
audio_language = ""       # "" = auto-detect; "en" / "zh" / etc.
audio_model_size = "base" # faster-whisper size: tiny / base / small / medium
# Camera capture — requires: pip install opencv-python-headless
camera_enabled = false    # set true to enable (opt-in)
camera_interval = 120     # seconds between camera snapshots
camera_motion_threshold = 0.05   # skip LLM when scene diff < this
# ML training: keep raw media on disk (default off — privacy)
audio_save_raw = false    # save WAV files to ~/.onion_shell/audio/
camera_save_frames = false # save JPEG frames to ~/.onion_shell/frames/
# Keyboard + mouse tracking (requires: pip install pynput + Accessibility permission on macOS)
input_enabled = true      # capture typed text segments + mouse clicks
"""


@dataclass
class MonitoringConfig:
    level: int = 3
    privacy: str = "standard"
    storage_gb: float = 2.0   # max disk space for kaiData (GB, up to 200)
    kaidata_path: str = ""    # "" = auto-detect (next to project)
    # CPU pressure thresholds: other_cpu% that triggers light / heavy / critical
    cpu_pressure_thresholds: list = field(default_factory=lambda: [50, 70, 85])


@dataclass
class ProvidersConfig:
    priority: list[str] = field(default_factory=lambda: ["ollama", "openai", "claude", "gemini"])
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "deepseek-r1:latest"
    ollama_vision_model: str = "moondream"      # background vision model (fast/lightweight)
    ollama_vision_model_hq: str = ""            # HQ vision at ask time (empty = fallback)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    google_api_key: str = ""
    google_model: str = "gemini/gemini-1.5-flash"


@dataclass
class SensorsConfig:
    audio_enabled: bool = False          # opt-in: microphone transcription
    audio_language: str = ""            # "" = auto-detect; "en", "zh", etc.
    audio_model_size: str = "base"      # faster-whisper model: tiny/base/small/medium
    camera_enabled: bool = False         # opt-in: webcam capture
    camera_interval: float = 120.0      # seconds between camera captures
    camera_motion_threshold: float = 0.05   # skip LLM if scene unchanged
    audio_save_raw: bool = False         # save raw WAV files for ML training
    camera_save_frames: bool = False     # save JPEG frames for ML training
    input_enabled: bool = True           # keyboard typing + mouse click tracking


@dataclass
class RetentionConfig:
    events_hours: float = 4.0
    checkpoints_hours: float = 24.0
    ocr_hours: float = 2.0


@dataclass
class Config:
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    sensors: SensorsConfig = field(default_factory=SensorsConfig)


def load() -> Config:
    """Load config from file, create default if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG)

    try:
        if tomllib is None:
            raw = {}
        else:
            raw = tomllib.loads(CONFIG_FILE.read_text())
    except Exception:
        raw = {}

    m = raw.get("monitoring", {})
    p = raw.get("providers", {})
    r = raw.get("retention", {})

    cfg = Config(
        monitoring=MonitoringConfig(
            level=int(os.environ.get("ONION_LEVEL", m.get("level", 3))),
            privacy=os.environ.get("ONION_PRIVACY", m.get("privacy", "standard")),
            storage_gb=float(os.environ.get("ONION_STORAGE_GB", m.get("storage_gb", 2.0))),
            kaidata_path=os.environ.get("ONION_KAIDATA_PATH", m.get("kaidata_path", "")),
        ),
        providers=ProvidersConfig(
            priority=[x.strip() for x in os.environ.get(
                "ONION_PRIORITY", p.get("priority", "ollama,openai,claude,gemini")
                if isinstance(p.get("priority"), str) else ",".join(p.get("priority", ["ollama"]))
            ).split(",")],
            ollama_url=os.environ.get("OLLAMA_URL", p.get("ollama_url", "http://localhost:11434")),
            ollama_model=os.environ.get("OLLAMA_MODEL", p.get("ollama_model", "deepseek-r1:latest")),
            ollama_vision_model=os.environ.get("OLLAMA_VISION_MODEL", p.get("ollama_vision_model", "moondream")),
            ollama_vision_model_hq=os.environ.get("OLLAMA_VISION_MODEL_HQ", p.get("ollama_vision_model_hq", "")),
            openai_api_key=os.environ.get("OPENAI_API_KEY", p.get("openai_api_key", "")),
            openai_model=os.environ.get("OPENAI_MODEL", p.get("openai_model", "gpt-4o-mini")),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", p.get("anthropic_api_key", "")),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", p.get("anthropic_model", "claude-haiku-4-5-20251001")),
            google_api_key=os.environ.get("GOOGLE_API_KEY", p.get("google_api_key", "")),
            google_model=os.environ.get("GOOGLE_MODEL", p.get("google_model", "gemini/gemini-1.5-flash")),
        ),
        retention=RetentionConfig(
            events_hours=float(r.get("events_hours", 4.0)),
            checkpoints_hours=float(r.get("checkpoints_hours", 24.0)),
            ocr_hours=float(r.get("ocr_hours", 2.0)),
        ),
        sensors=SensorsConfig(
            **{k: v for k, v in raw.get("sensors", {}).items()
               if k in SensorsConfig.__dataclass_fields__}
        ),
    )
    return cfg


# Runtime mutable: set by daemon's cpu_adjust_loop when user sets custom interval
CUSTOM_INTERVAL: int = 0   # 0 = use level-based defaults

# Per-level polling intervals — higher level = shorter interval = more granular tracking.
# app_poll: how often to check active app + window title + browser URL (seconds)
# ocr_fallback: how often to force an OCR screenshot even without title change
# vision_interval: how often to run vision model in browser/video apps (for auto mode)
# clip_poll: clipboard check interval
LEVEL_SETTINGS = {
    1: {"app_poll": 30,  "ocr_trigger": "never",         "ocr_fallback": None, "vision_interval": None, "clip_poll": 30},
    2: {"app_poll": 10,  "ocr_trigger": "app_switch",    "ocr_fallback": 120,  "vision_interval": 120,  "clip_poll": 10},
    3: {"app_poll": 3,   "ocr_trigger": "title_change",  "ocr_fallback": 60,   "vision_interval": 30,   "clip_poll": 5},
    4: {"app_poll": 2,   "ocr_trigger": "title_change",  "ocr_fallback": 30,   "vision_interval": 15,   "clip_poll": 3},
    5: {"app_poll": 1,   "ocr_trigger": "title_change",  "ocr_fallback": 15,   "vision_interval": 10,   "clip_poll": 2},
}

APP_CATEGORIES = {
    "code":    {"Code", "VSCode", "Visual Studio Code", "Xcode", "PyCharm", "Sublime Text", "Cursor", "Zed"},
    "browser": {"Safari", "Google Chrome", "Firefox", "Arc", "Brave Browser", "Opera", "Microsoft Edge"},
    "video":   {"VLC", "IINA", "QuickTime Player", "Infuse"},
    "privacy": {"1Password", "Keychain Access", "LastPass", "Bitwarden", "KeePass", "Dashlane"},
}
