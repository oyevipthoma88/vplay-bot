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


_REF_TTL = 15 * 60  # refresh the message (file reference) every 15 min


async def _fetch(chat_id: int, message_id: int, refresh: bool = False):
    key = (chat_id, message_id)
    cached = _cache.get(key)
    if cached and not refresh:
        age = time.time() - cached[2]
        if age < _CACHE_TTL:
            if age < _REF_TTL:
                return cached[0], cached[1]
            refresh = True
    for client in (bot, assistant):
        try:
            msg = await client.get_messages(chat_id, message_id)
            if msg and not getattr(msg, "empty", False) and _get_media(msg):
                _prune_cache()
                _cache[key] = (client, msg, time.time())
                return client, msg
        except Exception as e:  # noqa: BLE001
            logger.debug("streamer get_messages via %s failed: %s", client.name, e)
    if cached:
        return cached[0], cached[1]
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


# Read-ahead buffer per HTTP connection (in 1 MiB chunks). ROOT FIX for
# "beech me video/audio quality down ho jati hai": MTProto delivers chunks in
# bursts with short stalls. Without a buffer ffmpeg starves during a stall,
# PyTgCalls/WebRTC sees late frames and its congestion control drops the
# bitrate (blurry video, muddy audio) and only slowly climbs back. Fetching
# ahead keeps ffmpeg fed at a steady rate through those stalls.
PREFETCH_CHUNKS = max(4, int(getattr(config, "STREAM_PREFETCH_MB", 16) or 16))
_END = object()


async def _prefetch(gen, size: int = PREFETCH_CHUNKS):
    q: asyncio.Queue = asyncio.Queue(maxsize=size)

    async def _producer():
        try:
            async for c in gen:
                await q.put(c)
            await q.put(_END)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - re-raised in consumer
            await q.put(e)

    task = asyncio.create_task(_producer())
    try:
        while True:
            item = await q.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass


async def _pump(client, message, resp, start: int, length: int, file_size: int,
                sent=None) -> None:
    """Pump bytes, surviving mid-stream failures.

    Telegram file references expire during long movies; FloodWait also hits.
    Previously any exception ended the generator -> ffmpeg saw EOF -> audio
    died. Now we re-fetch the message and resume from the exact byte offset.
    """
    pos = int(start)
    end = int(start) + int(length) if file_size else None
    retries = 0
    while True:
        remaining = (end - pos) if end is not None else None
        if remaining is not None and remaining <= 0:
            return
        skip = pos % CHUNK_SIZE
        gen = client.stream_media(message, offset=pos // CHUNK_SIZE)
        try:
            async for chunk in _prefetch(gen):
                if skip:
                    chunk = chunk[skip:]
                    skip = 0
                if not chunk:
                    continue
                if remaining is not None and len(chunk) > remaining:
                    chunk = chunk[: int(remaining)]
                await resp.write(chunk)
                pos += len(chunk)
                if sent is not None:
                    sent[0] += len(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        return
            if end is None:
                return
            raise EOFError("stream ended early")
        except FloodWait as e:
            retries += 1
            if retries > 6:
                logger.warning("stream pump: FloodWait retries exhausted")
                return
            await asyncio.sleep(e.value + 1)
        except FileReferenceExpired:
            retries += 1
            if retries > 6:
                logger.warning("stream pump: file reference keeps expiring")
                return
            logger.info("stream pump: file reference expired — refreshing message")
            _cid = getattr(getattr(message, "chat", None), "id", None)
            _mid = getattr(message, "id", None)
            if _cid is None or _mid is None:
                return
            _cli, _msg = await _fetch(_cid, _mid, refresh=True)
            if _msg is None:
                return
            client, message = _cli, _msg
            await asyncio.sleep(0.5)
        except (ConnectionResetError, BrokenPipeError):
            return
        except (asyncio.TimeoutError, OSError) as e:
            retries += 1
            if retries > 6:
                logger.warning("stream pump: network retries exhausted (%s)", e)
                return
            await asyncio.sleep(min(2 ** retries, 10))
        except EOFError:
            retries += 1
            if retries > 6:
                logger.warning("stream pump: stream truncated repeatedly")
                return
            await asyncio.sleep(1)
        except Exception as e:  # noqa: BLE001
            logger.debug("stream pump ended: %s", e)
            return
        finally:
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
