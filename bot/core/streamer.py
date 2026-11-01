"""Local HTTP streamer: serves Telegram media to ffmpeg/py-tgcalls chunk by
chunk straight from Telegram (MTProto stream_media) with Range support.
Playback starts in ~0s — no full-file download to disk, so a 3.5 GB movie
starts as fast as a 5 MB clip, and seeking works via HTTP Range.

Root-cause notes (v1.3.1) — "Telegram file par /vplay chal hi nahi raha":

* Pyrogram/Kurigram allow only ONE active download per client by default
  (`max_concurrent_transmissions=1`, a semaphore held for the whole
  `stream_media` generator). PyTgCalls opens the URL several times at once
  (ffprobe for video + audio, then one ffmpeg for AUDIO and one for VIDEO),
  so the 2nd reader waited forever behind the 1st -> ffprobe timeouts,
  silent/black streams. The clients now allow parallel transmissions
  (see `clients.py`) and every request releases its slot immediately.
* Every HTTP request used to call `get_messages` again (4+ API round-trips
  before the first frame). The replied message is now registered up-front
  by `/vplay` and cached, and the file's media DC is warmed in background.
"""
import asyncio
import logging
import time

from aiohttp import web
from pyrogram.errors import FileReferenceExpired, FloodWait

from bot import config
from bot.core.clients import assistant, bot

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # Pyrogram stream_media offset unit (1 MiB)
_CACHE_TTL = 60 * 60  # file references stay valid for hours; refresh hourly
_runner = None
_PORT = config.STREAM_PORT

# (chat_id, message_id) -> (client, message, cached_at)
_cache: dict = {}


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


def _prune_cache() -> None:
    now = time.time()
    for key in [k for k, v in _cache.items() if now - v[2] > _CACHE_TTL * 6]:
        _cache.pop(key, None)


def remember(message, client=None) -> None:
    """Caches a message we already have (the one /vplay replied to) so the
    streamer can serve it without any extra Telegram API call, and warms up
    the media connection for the file's DC so the first chunk is instant.
    """
    if message is None or _get_media(message) is None:
        return
    client = client or bot
    key = (message.chat.id, message.id)
    _prune_cache()
    _cache[key] = (client, message, time.time())
    _warm_media_dc(client, _get_media(message))


def _warm_media_dc(client, media) -> None:
    try:
        from pyrogram.file_id import FileId

        dc_id = FileId.decode(media.file_id).dc_id
        get_session = getattr(client, "get_session", None)
        if callable(get_session):
            task = asyncio.ensure_future(get_session(dc_id, is_media=True))
            task.add_done_callback(lambda t: t.exception())  # silence "never retrieved"
    except Exception as e:  # noqa: BLE001 - warm-up is only an optimisation
        logger.debug("media DC warm-up skip: %s", e)


async def _fetch(chat_id: int, message_id: int, refresh: bool = False):
    key = (chat_id, message_id)
    cached = _cache.get(key)
    if cached and not refresh and time.time() - cached[2] < _CACHE_TTL:
        return cached[0], cached[1]

    for client in (bot, assistant):
        try:
            msg = await client.get_messages(chat_id, message_id)
            if msg and not getattr(msg, "empty", False) and _get_media(msg):
                _prune_cache()
                _cache[key] = (client, msg, time.time())
                return client, msg
        except Exception as e:  # noqa: BLE001
            logger.debug("streamer get_messages via %s failed: %s", client.name, e)
    return None, None


def _parse_range(rng: str, file_size: int):
    """Returns (start, end, status) or None for an unsatisfiable range."""
    start, end, status = 0, max(file_size - 1, 0), 200
    if rng.startswith("bytes="):
        a, _, b = rng[6:].split(",")[0].strip().partition("-")
        try:
            if a:
                start = int(a)
                if b:
                    end = min(int(b), file_size - 1) if file_size else int(b)
            elif b and file_size:  # suffix range: last N bytes
                start = max(0, file_size - int(b))
            status = 206 if file_size else 200
        except ValueError:
            start, status = 0, 200
    if file_size and (start >= file_size or end < start):
        return None
    return start, end, status


async def _pump(client, message, resp, start: int, length: int, file_size: int, sent=None) -> None:
    skip = start % CHUNK_SIZE
    remaining = length if file_size else float("inf")
    gen = client.stream_media(message, offset=start // CHUNK_SIZE)
    try:
        async for chunk in gen:
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                chunk = chunk[: int(remaining)]
            await resp.write(chunk)
            if sent is not None:
                sent[0] += len(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
    finally:
        # Release the client's download slot NOW (not whenever the garbage
        # collector gets to it) — ffmpeg reconnects on every seek.
        try:
            await gen.aclose()
        except Exception:  # noqa: BLE001
            pass


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

    parsed = _parse_range(request.headers.get("Range", ""), file_size)
    if parsed is None:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})
    start, end, status = parsed

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

    logger.info("Stream msg %s: %.1f MB, range %s-%s", message_id, file_size / 1e6, start, end)
    sent = [0]
    try:
        try:
            await _pump(client, message, resp, start, length, file_size, sent)
        except (asyncio.CancelledError, ConnectionResetError, FloodWait):
            raise
        except Exception as first:  # noqa: BLE001
            # Stale file reference, or the cached message belongs to the
            # other account (file refs are per-account). If nothing was
            # written yet, refetch via the OTHER client and retry once —
            # otherwise ffprobe gets an empty body and PyTgCalls crashes
            # with ProcessLookupError.
            if sent[0]:
                raise
            logger.info("Stream msg %s: %s — refetch + retry", message_id, first)
            _cache.pop((chat_id, message_id), None)
            other = assistant if client is bot else bot
            client, message = None, None
            for c in (other, assistant if other is bot else bot):
                try:
                    m = await c.get_messages(chat_id, message_id)
                    if m and not getattr(m, "empty", False) and _get_media(m):
                        client, message = c, m
                        break
                except Exception:  # noqa: BLE001
                    continue
            if message is None:
                raise first
            _cache[(chat_id, message_id)] = (client, message, time.time())
            await _pump(client, message, resp, start, length, file_size)
    except (asyncio.CancelledError, ConnectionResetError):
        pass  # ffmpeg seeked / closed — normal
    except FloodWait as fw:
        logger.warning("Streamer FloodWait %ss", fw.value)
    except Exception as e:  # noqa: BLE001
        if "closing transport" in str(e).lower() or "connection lost" in str(e).lower():
            pass  # client went away mid-write — normal
        else:
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
