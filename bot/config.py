"""All configuration is read from environment variables so the bot works
the same way locally (via a .env file) and on Heroku (via config vars).
"""
import os

from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file (e.g. on Heroku) — harmless


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

# --- Optional -----------------------------------------------------------
# Restrict playback-control commands (/vplay, /vskip, /vstop, ...) to group
# admins + OWNER_ID. Set to "false" to let anyone in the group control it.
AUTH_ONLY_ADMINS = _get_env_bool("AUTH_ONLY_ADMINS", default=True)

# Where downloaded/telegram media and temporary seek files are stored.
DOWNLOADS_DIR = _get_env_str("DOWNLOADS_DIR", default="downloads")

# One of: sd_480p, hd_720p, fhd_1080p
DEFAULT_VIDEO_QUALITY = _get_env_str("DEFAULT_VIDEO_QUALITY", default="sd_480p").lower()

# One of: low, medium, high
DEFAULT_AUDIO_QUALITY = _get_env_str("DEFAULT_AUDIO_QUALITY", default="high").lower()

# If the bot account doesn't receive a command (not in the group, privacy
# mode, ...), let the assistant account pick it up and run it instead.
ASSISTANT_FALLBACK = _get_env_bool("ASSISTANT_FALLBACK", default=False)

# Send a "bot started" DM to OWNER_ID on every start.
STARTUP_NOTIFY = _get_env_bool("STARTUP_NOTIFY", default=True)
