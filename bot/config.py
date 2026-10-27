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
DEFAULT_VIDEO_QUALITY = _get_env_str("DEFAULT_VIDEO_QUALITY", default="sd_480p").lower()
DEFAULT_AUDIO_QUALITY = _get_env_str("DEFAULT_AUDIO_QUALITY", default="high").lower()
ASSISTANT_FALLBACK = _get_env_bool("ASSISTANT_FALLBACK", default=False)
STARTUP_NOTIFY = _get_env_bool("STARTUP_NOTIFY", default=True)

# --- MongoDB / Logs / Streaming --------------------------------------
MONGO_DB_URI = _get_env_str("MONGO_DB_URI", default=None)
LOG_CHANNEL_ID = _get_env_int("LOG_CHANNEL_ID", default=None)
STREAM_PORT = _get_env_int("STREAM_PORT", default=8000)
