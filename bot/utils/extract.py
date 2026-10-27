"""Figures out *what* to play from a /vplay message: a direct http(s) link
after the command, or a video/audio/document the command replies to.

Telegram files are NOT downloaded anymore — they are served through the
local MTProto streamer, so playback starts in ~0 seconds.
"""
import re

from pyrogram.types import Message

from bot.core.streamer import get_stream_url
from bot.logger import logger

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


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
        return None
    return (
        replied.video
        or replied.audio
        or replied.document
        or replied.animation
        or replied.voice
        or replied.video_note
    )


async def resolve_source(message: Message):
    """Returns (source, title, is_url). Raises ValueError with a
    user-facing message if there's nothing usable to play.
    """
    url = _url_from_text(message.text or message.caption or "")
    if url:
        return url, url.rsplit("/", 1)[-1] or "Direct link", True

    media = _replied_media(message)
    text = (message.text or message.caption or "").strip()
    if media is None and len(text.split()) > 1:
        raise ValueError(
            "Song name se search supported nahi hai — sirf direct link (http...) "
            "ya kisi video/audio file par reply karke /vplay bhejein."
        )
    if media is None:
        raise ValueError(
            "Kisi video/audio file par reply karke /vplay bhejein, ya phir "
            "seedha `/vplay <direct link>` bhejein."
        )

    replied = message.reply_to_message
    title = (
        getattr(media, "file_name", None)
        or getattr(media, "title", None)
        or (replied.caption or "").split("\n")[0]
        or "Telegram media"
    )
    if len(title) > 60:
        title = title[:57] + "..."

    size_mb = (getattr(media, "file_size", 0) or 0) / 1e6
    logger.info("0s stream: msg %s (%s, %.1f MB) — no download", replied.id, title, size_mb)
    return get_stream_url(replied.chat.id, replied.id), title, True
