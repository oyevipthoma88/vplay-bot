"""Local HTTP streamer: serves Telegram media to ffmpeg/py-tgcalls chunk by
chunk straight from Telegram (MTProto stream_media) with Range support.
Playback starts in ~0s — no full-file download to disk, so a 3.5 GB movie
starts as fast as a 5 MB clip, and seeking works via HTTP Range.
"""
import asyncio
import logging

from aiohttp import web
from pyrogram.errors import FloodWait

from bot import config
from bot.core.clients import assistant, bot

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # Pyrogram stream_media offset unit (1 MiB)
_runner = None
_PORT = config.STREAM_PORT


def get_stream_url(chat_id: int, message_id: int) -> str:
    return f"http://127.0.0.1:{_PORT}/stream/{chat_id}/{message_id}"


def _get_media(message):
    return (
        getattr(message, "video", None)
        or getattr(message, "document", None)
        or getattr(message, "audio", None)
        or getattr(message, "animation", None)
        or getattr(message, "voice", None)
        or getattr(message, "video_note", None)
    )


async def _fetch(chat_id: int, message_id: int):
    for client in (bot, assistant):
        try:
            msg = await client.get_messages(chat_id, message_id)
            if msg and not getattr(msg, "empty", False) and _get_media(msg):
                return client, msg
        except Exception as e:  # noqa: BLE001
            logger.debug("streamer get_messages via %s failed: %s", client.name, e)
    return None, None


async def _handle_stream(request: web.Request) -> web.StreamResponse:
    try:
        chat_id = int(request.match_info["chat_id"])
        message_id = int(request.match_info["message_id"])
    except (KeyError, ValueError):
        return web.Response(status=400, text="bad ids")

    client, message = await _fetch(chat_id, message_id)
    if message is None:
        return web.Response(status=404, text="media not found")

    media = _get_media(message)
    file_size = getattr(media, "file_size", 0) or 0
    mime = getattr(media, "mime_type", None) or "video/mp4"

    start, end, status = 0, max(file_size - 1, 0), 200
    rng = request.headers.get("Range", "")
    if rng.startswith("bytes="):
        a, _, b = rng[6:].partition("-")
        try:
            if a:
                start = int(a)
                if b:
                    end = min(int(b), file_size - 1)
            elif b and file_size:  # suffix range: last N bytes
                start = max(0, file_size - int(b))
            status = 206 if file_size else 200
        except ValueError:
            start = 0
    if file_size and (start >= file_size or end < start):
        return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1 if file_size else 0
    headers = {"Content-Type": mime, "Accept-Ranges": "bytes"}
    if file_size:
        headers["Content-Length"] = str(length)
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    if request.method == "HEAD":
        return web.Response(status=status, headers=headers)

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)

    skip = start % CHUNK_SIZE
    remaining = length if file_size else float("inf")
    logger.info("Stream msg %s: %.1f MB, range %s-%s", message_id, file_size / 1e6, start, end)
    try:
        async for chunk in client.stream_media(message, offset=start // CHUNK_SIZE):
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                chunk = chunk[: int(remaining)]
            await resp.write(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
    except (asyncio.CancelledError, ConnectionResetError):
        pass  # ffmpeg seeked / closed — normal
    except FloodWait as fw:
        logger.warning("Streamer FloodWait %ss", fw.value)
    except Exception as e:  # noqa: BLE001
        logger.warning("Streamer error: %s", e)
    return resp


async def start_streamer() -> None:
    global _runner
    if _runner:
        return
    app = web.Application()
    app.router.add_get("/stream/{chat_id}/{message_id}", _handle_stream)  # also serves HEAD
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, "127.0.0.1", _PORT).start()
    logger.info("0s streamer chal raha hai: http://127.0.0.1:%s", _PORT)


async def stop_streamer() -> None:
    global _runner
    if _runner:
        await _runner.cleanup()
        _runner = None
