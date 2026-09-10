"""Figures out *what* to play from a /vplay message: either a direct
http(s) link typed after the command, or a video/audio/document/etc. that
the command is a reply to. Deliberately does NOT do any YouTube search or
yt-dlp style extraction — only media the user directly hands over, which
means no cookies/auth are ever needed.
"""
import os
import re
import uuid

from pyrogram.types import Message

from bot import config

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _url_from_text(text: str):
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    candidate = parts[1].strip()
    if _URL_RE.match(candidate):
        return candidate
    return None


def _replied_media(message: Message):
    if not message.reply_to_message:
        return None
    replied = message.reply_to_message
    return (
        replied.video
        or replied.audio
        or replied.document
        or replied.animation
        or replied.voice
        or replied.video_note
    )


def _guess_extension(media) -> str:
    file_name = getattr(media, "file_name", None)
    if file_name and "." in file_name:
        return "." + file_name.rsplit(".", 1)[-1]
    mime = getattr(media, "mime_type", "") or ""
    if "video" in mime:
        return ".mp4"
    if "audio" in mime:
        return ".mp3"
    return ".bin"


async def resolve_source(message: Message):
    """Returns (source, title, is_url). Raises ValueError with a
    user-facing message if there's nothing usable to play.
    """
    url = _url_from_text(message.text or message.caption or "")
    if url:
        title = url.rsplit("/", 1)[-1] or "Direct link"
        return url, title, True

    media = _replied_media(message)
    if media is None:
        raise ValueError(
            "Kisi video/audio file par reply karke /vplay bhejein, ya phir "
            "seedha `/vplay <direct link>` bhejein."
        )

    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)
    local_name = f"{uuid.uuid4().hex}{_guess_extension(media)}"
    local_path = os.path.join(config.DOWNLOADS_DIR, local_name)
    downloaded_path = await message.reply_to_message.download(file_name=local_path)

    title = getattr(media, "file_name", None) or "Telegram media"
    return downloaded_path, title, False
