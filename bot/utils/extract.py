"""Figures out *what* to play from a /vplay message: a direct http(s) link
after the command, or a video/audio/document the command replies to.

Telegram files are NOT downloaded — they are served through the local
MTProto streamer, so playback starts in ~0 seconds.
"""
import re
from urllib.parse import unquote

from pyrogram.types import Message

from bot.core import streamer
from bot.core.queue import Track
from bot.core.streamer import get_stream_url
from bot.logger import logger

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav")


def _url_from_text(text: str):
    if not text:
        return None
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    candidate = parts[1].strip()
    return candidate if _URL_RE.match(candidate) else None


def _replied_media(message: Message):
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return None, None
    for kind in ("video", "audio", "document", "animation", "voice", "video_note"):
        media = getattr(replied, kind, None)
        if media:
            return media, kind
    return None, None


def _short(title: str, limit: int = 60) -> str:
    title = (title or "").strip() or "Telegram media"
    return title if len(title) <= limit else title[: limit - 3] + "..."


async def resolve_track(message: Message, requested_by: str, requested_by_id: int) -> Track:
    """Builds a Track. Raises ValueError with a user-facing message if
    there's nothing usable to play.
    """
    url = _url_from_text(message.text or message.caption or "")
    if url:
        name = unquote(url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]) or "Direct link"
        return Track(
            source=url, title=_short(name), requested_by=requested_by,
            requested_by_id=requested_by_id, is_url=True, mode="link",
            is_video=not name.lower().endswith(_AUDIO_EXT),
        )

    media, kind = _replied_media(message)
    text = (message.text or message.caption or "").strip()
    if media is None and len(text.split()) > 1:
        raise ValueError(
            "Naam se search supported nahi hai — direct link (http...) do, "
            "ya kisi video/audio file par reply karke /vplay bhejo."
        )
    if media is None:
        raise ValueError(
            "Kisi video/audio file par reply karke /vplay bhejo, "
            "ya phir /vplay <direct link> bhejo."
        )

    mime = (getattr(media, "mime_type", "") or "").lower()
    if kind == "document" and mime and not (mime.startswith("video") or mime.startswith("audio")):
        raise ValueError("Ye file video/audio nahi lagti. Koi video ya audio file bhejo.")

    replied = message.reply_to_message
    title = (
        getattr(media, "title", None)
        or getattr(media, "file_name", None)
        or (replied.caption or "").split("\n")[0]
        or "Telegram media"
    )
    is_video = kind in ("video", "animation", "video_note") or mime.startswith("video")
    duration = int(getattr(media, "duration", 0) or 0)

    # Hand the replied message straight to the streamer: no extra API call
    # per HTTP request, and the file's DC connection starts warming now.
    streamer.remember(replied)

    size_mb = (getattr(media, "file_size", 0) or 0) / 1e6
    logger.info("0s stream: msg %s (%s, %.1f MB) — no download", replied.id, title, size_mb)
    return Track(
        source=get_stream_url(replied.chat.id, replied.id), title=_short(title),
        requested_by=requested_by, requested_by_id=requested_by_id, is_url=True,
        duration=duration, mode="stream", is_video=is_video,
    )
