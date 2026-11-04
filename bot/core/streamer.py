"""Local HTTP streamer: serves Telegram media to ffmpeg/py-tgcalls straight
from Telegram (MTProto) with HTTP Range support — no full download, so a
3 GB movie starts as fast as a 5 MB clip.

v1.5 — shared chunk cache (ROOT FIX for "beech me quality down ho jati hai"
and "kabhi kabhi jaldi play nahi hota"):

* PyTgCalls opens the same URL 4-5 times (our ffprobe, its own ffprobe for
  audio + video, then one ffmpeg for AUDIO and one for VIDEO). Earlier every
  HTTP request started its OWN Telegram download, so the same movie was
  downloaded twice in parallel during playback. Both downloads competed for
  the same MTProto connection, stalled in turns, ffmpeg starved and the
  WebRTC encoder dropped bitrate (blurry video / muddy audio).
* Now every file has ONE shared, bounded chunk cache. Readers that ask for
  the same 1 MiB chunk share a single download; a background worker keeps
  STREAM_PREFETCH_MB ahead of the furthest reader. The audio and video
  ffmpeg therefore read from RAM, Telegram is downloaded only once, and the
  start-up probes reuse the same chunks (much faster start).
* /vplay warms the first chunk (and the last one for MP4, where the index
  usually lives) while the bot is still joining the video chat.
"""
import asyncio
import logging
import time
from collections import OrderedDict
from typing import Dict, List, Optional

from aiohttp import web
from pyrogram.errors import FileReferenceExpired, FloodWait

from bot import config
from bot.core.clients import assistant, bot

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # Pyrogram stream_media unit (1 MiB)
_PORT = config.STREAM_PORT
_runner = None

# Chunks fetched ahead of the furthest reader.
PREFETCH_CHUNKS = max(4, int(getattr(config, "STREAM_PREFETCH_MB", 16) or 16))
# Max chunks kept in RAM per file (must cover the audio/video reader gap).
CACHE_CHUNKS = max(PREFETCH_CHUNKS * 2 + 8, int(getattr(config, "STREAM_CACHE_MB", 48) or 48))
_READ_TIMEOUT = 25        # seconds a reader waits for one chunk before retrying
_STORE_IDLE_TTL = 10 * 60  # drop a file's cache after 10 min without readers
_REF_TTL = 15 * 60        # refresh the message (file reference) every 15 min
_MSG_TTL = 6 * 60 * 60

# (chat_id, message_id) -> (client, message, cached_at)
_msg_cache: dict = {}
# (chat_id, message_id) -> _FileStore
_stores: Dict[tuple, "_FileStore"] = {}


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


def _prune() -> None:
    now = time.time()
    for key in [k for k, v in _msg_cache.items() if now - v[2] > _MSG_TTL]:
        _msg_cache.pop(key, None)
    for key in [k for k, s in _stores.items() if s.idle(now)]:
        _stores.pop(key, None)


# --------------------------------------------------------------- messages
def remember(message, client=None) -> None:
    """Cache a message we already have so the streamer needs no extra API
    call, and start downloading the first chunk(s) right away."""
    if message is None or _get_media(message) is None:
        return
    client = client or bot
    key = (message.chat.id, message.id)
    _prune()
    _msg_cache[key] = (client, message, time.time())
    media = _get_media(message)
    _warm_media_dc(client, media)
    try:
        store = _store_for(key, getattr(media, "file_size", 0) or 0)
        store.want(0)
        mime = (getattr(media, "mime_type", "") or "").lower()
        if store.total and store.total > 2 and ("mp4" in mime or "quicktime" in mime):
            store.want(store.total - 1)  # moov atom usually at the end
    except RuntimeError:
        pass  # no running loop (import time) — fine


def _warm_media_dc(client, media) -> None:
    try:
        from pyrogram.file_id import FileId

        dc_id = FileId.decode(media.file_id).dc_id
        get_session = getattr(client, "get_session", None)
        if callable(get_session):
            task = asyncio.ensure_future(get_session(dc_id, is_media=True))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
    except Exception as e:  # noqa: BLE001 - optimisation only
        logger.debug("media DC warm-up skip: %s", e)


async def _fetch(chat_id: int, message_id: int, refresh: bool = False, avoid=None):
    """Returns (client, message). `avoid` = client to try LAST (it failed)."""
    key = (chat_id, message_id)
    cached = _msg_cache.get(key)
    if cached and not refresh and avoid is None:
        if time.time() - cached[2] < _REF_TTL:
            return cached[0], cached[1]
    order: List = [bot, assistant]
    if cached and cached[0] in order and avoid is None:
        order.remove(cached[0])
        order.insert(0, cached[0])
    if avoid in order:
        order.remove(avoid)
        order.append(avoid)
    for client in order:
        try:
            msg = await client.get_messages(chat_id, message_id)
            if msg and not getattr(msg, "empty", False) and _get_media(msg):
                _msg_cache[key] = (client, msg, time.time())
                return client, msg
        except FloodWait as e:
            await asyncio.sleep(min(e.value, 30) + 1)
        except Exception as e:  # noqa: BLE001
            logger.debug("streamer get_messages via %s failed: %s", client.name, e)
    if cached:
        return cached[0], cached[1]
    return None, None


# ------------------------------------------------------------ chunk store
class _Worker:
    __slots__ = ("next", "until", "task")

    def __init__(self, start: int, until: int):
        self.next = start
        self.until = until
        self.task = None


class _FileStore:
    """Shared, bounded chunk cache + sequential prefetch workers for ONE file."""

    def __init__(self, key: tuple, file_size: int):
        self.key = key
        self.size = int(file_size or 0)
        self.total: Optional[int] = (self.size + CHUNK_SIZE - 1) // CHUNK_SIZE if self.size else None
        self.eof: Optional[int] = None
        self.chunks: "OrderedDict[int, bytes]" = OrderedDict()
        self.waiters: Dict[int, asyncio.Future] = {}
        self.workers: List[_Worker] = []
        self.readers = 0
        self.last_used = time.time()

    def idle(self, now: float) -> bool:
        return not self.readers and not self.workers and now - self.last_used > _STORE_IDLE_TTL

    def _limit(self) -> Optional[int]:
        return self.total if self.total is not None else self.eof

    def _put(self, idx: int, data: bytes) -> None:
        self.chunks[idx] = data
        self.chunks.move_to_end(idx)
        while len(self.chunks) > CACHE_CHUNKS:
            self.chunks.popitem(last=False)
        fut = self.waiters.pop(idx, None)
        if fut is not None and not fut.done():
            fut.set_result(data)

    def _fail(self, lo: int, hi: int, exc: BaseException) -> None:
        for i in [i for i in self.waiters if lo <= i < hi]:
            fut = self.waiters.pop(i)
            if not fut.done():
                fut.set_exception(exc)

    def _mark_eof(self, idx: int) -> None:
        self.eof = idx
        for i in [i for i in self.waiters if i >= idx]:
            fut = self.waiters.pop(i)
            if not fut.done():
                fut.set_result(b"")

    def want(self, idx: int) -> None:
        """Make sure some worker fetches `idx` and the window after it."""
        limit = self._limit()
        if limit is not None and idx >= limit:
            return
        until = idx + PREFETCH_CHUNKS
        if limit is not None:
            until = min(until, limit)
        for w in self.workers:
            if w.next <= idx <= w.next + PREFETCH_CHUNKS:
                if until > w.until:
                    w.until = until
                return
        # Everything in the window already cached -> nothing to do.
        if all(i in self.chunks for i in range(idx, until)):
            return
        w = _Worker(idx, until)
        self.workers.append(w)
        w.task = asyncio.ensure_future(self._run(w))

    async def get(self, idx: int) -> bytes:
        self.last_used = time.time()
        limit = self._limit()
        if limit is not None and idx >= limit:
            return b""
        data = self.chunks.get(idx)
        if data is not None:
            self.chunks.move_to_end(idx)
            self.want(idx + 1)
            return data
        fut = self.waiters.get(idx)
        if fut is None:
            fut = asyncio.get_event_loop().create_future()
            fut.add_done_callback(lambda f: f.cancelled() or f.exception())
            self.waiters[idx] = fut
        self.want(idx)
        return await asyncio.wait_for(asyncio.shield(fut), timeout=_READ_TIMEOUT)

    async def _run(self, w: _Worker) -> None:
        retries = 0
        client = None
        try:
            while w.next < w.until:
                while w.next < w.until and w.next in self.chunks:
                    w.next += 1
                if w.next >= w.until:
                    break
                # Merge into another worker that is already at this position.
                for o in self.workers:
                    if o is not w and o.next <= w.next <= o.next + 2:
                        o.until = max(o.until, w.until)
                        return
                client, message = await _fetch(*self.key)
                if message is None:
                    raise RuntimeError("media not found")
                gen = client.stream_media(message, offset=w.next)
                got_any = False
                try:
                    async for chunk in gen:
                        got_any = True
                        self._put(w.next, chunk)
                        w.next += 1
                        retries = 0
                        if w.next >= w.until or w.next in self.chunks:
                            break
                    else:
                        # Generator finished on its own -> real end of file.
                        if self.total is None or w.next >= self.total:
                            self._mark_eof(w.next)
                            return
                        if not got_any:
                            raise EOFError("empty read")
                except FloodWait as e:
                    retries += 1
                    if retries > 6:
                        raise
                    logger.info("streamer FloodWait %ss (msg %s)", e.value, self.key[1])
                    await asyncio.sleep(e.value + 1)
                except FileReferenceExpired:
                    retries += 1
                    if retries > 6:
                        raise
                    logger.info("streamer: file reference expired — refreshing msg %s", self.key[1])
                    await _fetch(*self.key, refresh=True)
                except (asyncio.TimeoutError, OSError, EOFError, ConnectionError) as e:
                    retries += 1
                    if retries > 6:
                        raise
                    await asyncio.sleep(min(2 ** retries, 10))
                    if retries >= 3:
                        await _fetch(*self.key, avoid=client)  # try the other account
                    logger.info("streamer retry %s (msg %s): %s", retries, self.key[1], type(e).__name__)
                finally:
                    try:
                        await gen.aclose()
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("streamer worker gave up (msg %s @%s): %s", self.key[1], w.next, e)
            self._fail(w.next, w.until + 1, e)
        finally:
            try:
                self.workers.remove(w)
            except ValueError:
                pass


def _store_for(key: tuple, file_size: int) -> _FileStore:
    store = _stores.get(key)
    if store is None or (file_size and store.size != file_size):
        store = _FileStore(key, file_size)
        _stores[key] = store
    return store


# ------------------------------------------------------------------- HTTP
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


async def _pump(store: _FileStore, resp, start: int, end: Optional[int]) -> None:
    pos = start
    timeouts = 0
    while end is None or pos <= end:
        idx = pos // CHUNK_SIZE
        try:
            data = await store.get(idx)
        except asyncio.TimeoutError:
            timeouts += 1
            if timeouts > 3:
                logger.warning("streamer: chunk %s of msg %s timed out", idx, store.key[1])
                return
            continue
        timeouts = 0
        if not data:
            return  # EOF
        off = pos - idx * CHUNK_SIZE
        piece = data[off:] if end is None else data[off: off + (end - pos + 1)]
        if not piece:
            return
        await resp.write(piece)
        pos += len(piece)


async def _handle_stream(request: web.Request) -> web.StreamResponse:
    try:
        chat_id = int(request.match_info["chat_id"])
        message_id = int(request.match_info["message_id"])
    except (KeyError, ValueError):
        return web.Response(status=400, text="bad ids")

    _client, message = await _fetch(chat_id, message_id)
    if message is None:
        return web.Response(status=404, text="media not found")

    media = _get_media(message)
    file_size = getattr(media, "file_size", 0) or 0
    mime = getattr(media, "mime_type", None) or "video/mp4"

    parsed = _parse_range(request.headers.get("Range", ""), file_size)
    if parsed is None:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})
    start, end, status = parsed

    headers = {"Content-Type": mime, "Accept-Ranges": "bytes"}
    if file_size:
        headers["Content-Length"] = str(end - start + 1)
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    if request.method == "HEAD":
        return web.Response(status=status, headers=headers)

    _prune()
    store = _store_for((chat_id, message_id), file_size)
    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)
    logger.info("Stream msg %s: %.1f MB, range %s-%s", message_id, file_size / 1e6, start, end)

    store.readers += 1
    try:
        await _pump(store, resp, start, end if file_size else None)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass  # ffmpeg seeked / closed — normal
    except Exception as e:  # noqa: BLE001
        low = str(e).lower()
        if "closing transport" not in low and "connection lost" not in low:
            logger.warning("Streamer error (msg %s): %s", message_id, e)
    finally:
        store.readers -= 1
        store.last_used = time.time()
    return resp


def stats() -> dict:
    return {
        "files": len(_stores),
        "cached_mb": sum(len(s.chunks) for s in _stores.values()),
        "workers": sum(len(s.workers) for s in _stores.values()),
        "readers": sum(s.readers for s in _stores.values()),
    }


async def start_streamer() -> None:
    global _runner
    if _runner:
        return
    app = web.Application()
    app.router.add_get("/stream/{chat_id}/{message_id}", _handle_stream)  # also serves HEAD
    _runner = web.AppRunner(app)
    await _runner.setup()
    await web.TCPSite(_runner, "127.0.0.1", _PORT).start()
    logger.info(
        "0s streamer chal raha hai: http://127.0.0.1:%s (prefetch %s MB, cache %s MB/file)",
        _PORT, PREFETCH_CHUNKS, CACHE_CHUNKS,
    )


async def stop_streamer() -> None:
    global _runner
    for s in list(_stores.values()):
        for w in list(s.workers):
            if w.task is not None:
                w.task.cancel()
    if _runner:
        await _runner.cleanup()
        _runner = None
