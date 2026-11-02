"""All configuration is read from environment variables so the bot works
the same way locally (via a .env file) and on Heroku (via config vars).
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _get_env_int(name: str, default=None, required: bool = False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got: {value!r}")


def _get_env_str(name: str, default=None, required: bool = False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    return value.strip()


def _get_env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# --- Required ---------------------------------------------------------
API_ID = _get_env_int("API_ID", required=True)
API_HASH = _get_env_str("API_HASH", required=True)
BOT_TOKEN = _get_env_str("BOT_TOKEN", required=True)
SESSION_STRING = _get_env_str("SESSION_STRING", required=True)
OWNER_ID = _get_env_int("OWNER_ID", required=True)

# --- Optional ---------------------------------------------------------
AUTH_ONLY_ADMINS = _get_env_bool("AUTH_ONLY_ADMINS", default=True)
DOWNLOADS_DIR = _get_env_str("DOWNLOADS_DIR", default="downloads")
DEFAULT_VIDEO_QUALITY = _get_env_str("DEFAULT_VIDEO_QUALITY", default="hd_720p").lower()
DEFAULT_AUDIO_QUALITY = _get_env_str("DEFAULT_AUDIO_QUALITY", default="high").lower()
AUDIO_CHANNELS = _get_env_int("AUDIO_CHANNELS", default=2)  # 1=mono, 2=stereo
VIDEO_FPS = _get_env_int("VIDEO_FPS", default=30)  # Telegram max=30
FFMPEG_THREADS = _get_env_int("FFMPEG_THREADS", default=2)
FFMPEG_READRATE = _get_env_str("FFMPEG_READRATE", default="0")   # 0 = off (recommended)
FFMPEG_PROBE_SIZE = _get_env_int("FFMPEG_PROBE_SIZE", default=3000000)
FFMPEG_ANALYZE_US = _get_env_int("FFMPEG_ANALYZE_US", default=3000000)
FFMPEG_RW_TIMEOUT_US = _get_env_int("FFMPEG_RW_TIMEOUT_US", default=8000000)
ASSISTANT_FALLBACK = _get_env_bool("ASSISTANT_FALLBACK", default=True)
STARTUP_NOTIFY = _get_env_bool("STARTUP_NOTIFY", default=True)


# --- Branding / links -------------------------------------------------
UPDATE_CHANNEL = _get_env_str("UPDATE_CHANNEL", default=None)
SUPPORT_GROUP = _get_env_str("SUPPORT_GROUP", default=None)
OWNER_USERNAME = _get_env_str("OWNER_USERNAME", default=None)


def _normalize_link(value, username=None):
    """Return a valid t.me URL or None. Accepts @name / name / full URL."""
    if value:
        v = value.strip()
        if v.startswith("http://") or v.startswith("https://"):
            return v
        if v.startswith("tg://"):
            return None
        v = v.lstrip("@")
        if v:
            return f"https://t.me/{v}"
    if username:
        return f"https://t.me/{username.lstrip('@')}"
    return None


SUPPORT_URL = _normalize_link(SUPPORT_GROUP)
UPDATE_URL = _normalize_link(UPDATE_CHANNEL)
OWNER_URL = _normalize_link(None, OWNER_USERNAME)
if SUPPORT_GROUP and not SUPPORT_URL:
    import logging as _lg
    _lg.getLogger(__name__).warning(
        "SUPPORT_GROUP=%r invalid — Support button in-bot help use karega", SUPPORT_GROUP
    )

# --- MongoDB / Logs / Streaming --------------------------------------
MONGO_DB_URI = _get_env_str("MONGO_DB_URI", default=None)
LOG_CHANNEL_ID = _get_env_int("LOG_CHANNEL_ID", default=None)
STREAM_PORT = _get_env_int("STREAM_PORT", default=8000)

# --- Movie channel search ---------------------------------------------
MOVIE_CHANNEL = _get_env_str("MOVIE_CHANNEL", default=None)       # -100... or @username
MOVIE_CHANNEL_ID = _get_env_int("MOVIE_CHANNEL_ID", default=None)  # numeric id
MOVIE_SEARCH_ENABLED = _get_env_bool("MOVIE_SEARCH_ENABLED", default=True)
MOVIE_CHANNELS = _get_env_str("MOVIE_CHANNELS", default="")   # comma-separated extra channels
MOVIE_FUZZY_THRESHOLD = _get_env_int("MOVIE_FUZZY_THRESHOLD", default=60)
MOVIE_SORT_MODE = _get_env_str("MOVIE_SORT_MODE", default="latest").lower()
MOVIE_SCAN_LIMIT = _get_env_int("MOVIE_SCAN_LIMIT", default=1500)
MOVIE_GLOBAL_LIMIT = _get_env_int("MOVIE_GLOBAL_LIMIT", default=25)

