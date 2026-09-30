"""Wraps PyTgCalls so the command handlers stay simple. Also implements
seeking, which PyTgCalls itself does not expose directly: we cut a fresh
local file starting at the requested offset with ffmpeg (fast stream-copy
first, falling back to a re-encode if the source can't be copy-seeked),
then hand that file to PyTgCalls like any other track.

PyTgCalls renamed several methods between 1.x and 2.x (`join_group_call` ->
`play`, `change_stream` -> `play`, `leave_group_call` -> `leave_call`,
`mute_stream` -> `mute`, ...). Rather than pinning ourselves to one exact
release, every call goes through `_safe_call`, which tries the known
aliases in order and only fails once none of them exist.
"""
import asyncio
import os
import time
import uuid
from typing import Optional

from pytgcalls.types import AudioQuality, MediaStream, VideoQuality

from bot import config
from bot.core.clients import assistant, bot, call_py
from bot.core.queue import Track, clear_state, get_state
from bot.logger import logger

_VIDEO_QUALITY = {
    "sd_480p": "SD_480p",
    "hd_720p": "HD_720p",
    "fhd_1080p": "FHD_1080p",
}

# Method-name aliases across PyTgCalls versions, most recent name first.
_METHOD_ALIASES = {
    "play": ("play", "join_group_call", "change_stream"),
    "leave": ("leave_call", "leave_group_call"),
    "pause": ("pause", "pause_stream"),
    "resume": ("resume", "resume_stream"),
    "mute": ("mute", "mute_stream"),
    "unmute": ("unmute", "unmute_stream"),
}

# Serialises playback changes per chat so two commands racing (e.g. /vskip
# fired twice) can't both mutate the same chat's state halfway through.
_locks: dict = {}


def _lock(chat_id: int) -> asyncio.Lock:
    lock = _locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[chat_id] = lock
    return lock


def _video_quality():
    attr = _VIDEO_QUALITY.get(config.DEFAULT_VIDEO_QUALITY, "SD_480p")
    return getattr(VideoQuality, attr, VideoQuality.SD_480p)


def _audio_quality():
    if config.DEFAULT_AUDIO_QUALITY == "low":
        return AudioQuality.LOW
    if config.DEFAULT_AUDIO_QUALITY == "medium":
        return AudioQuality.MEDIUM
    return AudioQuality.HIGH


async def _safe_call(action: str, *args):
    """Calls the first PyTgCalls method matching `action`'s known aliases."""
    for name in _METHOD_ALIASES.get(action, (action,)):
        method = getattr(call_py, name, None)
        if callable(method):
            return await method(*args)
    raise RuntimeError(
        f"PyTgCalls me `{action}` ke liye koi known method nahi mila "
        f"(tried: {', '.join(_METHOD_ALIASES.get(action, (action,)))}). "
        f"Installed `py-tgcalls` version requirements.txt se match nahi kar raha."
    )


async def _start_stream(chat_id: int, track: Track) -> None:
    stream = MediaStream(track.source, _audio_quality(), _video_quality())
    logger.info("Chat %s: stream start — %s", chat_id, track.title)
    try:
        await _safe_call("play", chat_id, stream)
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001 - genuinely need the broad catch here
        logger.exception("Chat %s: play failed", chat_id)
        raise RuntimeError(
            "Video/voice chat se connect nahi ho paya. Check karein ki group "
            "ka video ya voice chat abhi active hai, aur assistant account us "
            "group mein admin hai (Manage Video Chats permission ke saath). "
            f"(Technical: {e})"
        ) from e

    state = get_state(chat_id)
    state.current = track
    state.started_at = time.time()
    state.paused_duration = 0.0
    state.is_paused = False
    state.paused_at = None


async def warm_up_assistant_peers() -> int:
    """The assistant session is in-memory, so after every restart it has no
    cached access hashes and cannot resolve group ids (PEER_ID_INVALID)
    until it has seen them. Walking the dialog list fills that cache.
    """
    count = 0
    try:
        async for _ in assistant.get_dialogs():
            count += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("Assistant dialogs load nahi ho paye: %s", e)
    return count


async def ensure_assistant_in_chat(chat_id: int) -> None:
    """Makes sure the assistant is a member of `chat_id`, joining through a
    bot-generated invite link if it isn't.
    """
    from pyrogram.errors import UserAlreadyParticipant, UserNotParticipant

    async def _is_member() -> bool:
        await assistant.get_chat_member(chat_id, "me")
        return True

    try:
        if await _is_member():
            return
    except UserNotParticipant:
        pass
    except Exception as e:  # noqa: BLE001 - usually an unknown peer after restart
        logger.info("Assistant chat %s resolve nahi kar paya (%s) — dialogs reload", chat_id, e)
        await warm_up_assistant_peers()
        try:
            if await _is_member():
                return
        except Exception:  # noqa: BLE001
            pass

    logger.info("Assistant chat %s me nahi hai — invite link se join karwa raha hoon", chat_id)
    try:
        link = await bot.export_chat_invite_link(chat_id)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Assistant account is group me nahi hai, aur bot invite link nahi bana paya. "
            "Assistant ko manually group me add karein, ya bot ko admin banakar "
            "'Invite users via link' permission dein."
        ) from e
    try:
        await assistant.join_chat(link)
    except UserAlreadyParticipant:
        pass
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Assistant group join nahi kar paya: {e}") from e
    await asyncio.sleep(1)


async def play_or_queue(chat_id: int, track: Track) -> bool:
    """Starts playing immediately if nothing is playing yet, otherwise adds
    to the queue. Returns True if playback started immediately.
    """
    async with _lock(chat_id):
        state = get_state(chat_id)
        if state.current is None:
            await _start_stream(chat_id, track)
            return True
        state.queue.append(track)
        return False


async def _advance(chat_id: int) -> Optional[Track]:
    """Drops the current track and starts the next queued one (if any)."""
    state = get_state(chat_id)
    _cleanup_file(state.current)
    state.current = None

    while state.queue:
        next_track = state.queue.pop(0)
        try:
            await _start_stream(chat_id, next_track)
            return next_track
        except Exception as e:  # noqa: BLE001 - skip a broken track, keep going
            logger.warning("Track '%s' play nahi ho paya, skipping: %s", next_track.title, e)
            _cleanup_file(next_track)

    try:
        await _safe_call("leave", chat_id)
    except Exception:  # noqa: BLE001 - best-effort, state is cleared regardless
        pass
    clear_state(chat_id)
    _locks.pop(chat_id, None)
    return None


async def skip(chat_id: int) -> Optional[Track]:
    async with _lock(chat_id):
        return await _advance(chat_id)


async def stop(chat_id: int) -> None:
    async with _lock(chat_id):
        state = get_state(chat_id)
        _cleanup_file(state.current)
        for track in state.queue:
            _cleanup_file(track)
        try:
            await _safe_call("leave", chat_id)
        except Exception:  # noqa: BLE001 - best-effort
            pass
        clear_state(chat_id)
    _locks.pop(chat_id, None)


async def pause(chat_id: int) -> None:
    state = get_state(chat_id)
    if state.current is None:
        raise RuntimeError("Abhi kuch play nahi ho raha hai.")
    if state.is_paused:
        raise RuntimeError("Playback pehle se hi paused hai.")
    await _safe_call("pause", chat_id)
    state.is_paused = True
    state.paused_at = time.time()


async def resume(chat_id: int) -> None:
    state = get_state(chat_id)
    if state.current is None:
        raise RuntimeError("Abhi kuch play nahi ho raha hai.")
    if not state.is_paused:
        raise RuntimeError("Playback already chal raha hai.")
    await _safe_call("resume", chat_id)
    if state.paused_at:
        state.paused_duration += time.time() - state.paused_at
    state.is_paused = False
    state.paused_at = None


async def mute(chat_id: int) -> None:
    await _safe_call("mute", chat_id)


async def unmute(chat_id: int) -> None:
    await _safe_call("unmute", chat_id)


async def seek(chat_id: int, seconds: int) -> None:
    async with _lock(chat_id):
        state = get_state(chat_id)
        track = state.current
        if track is None:
            raise RuntimeError("Abhi kuch play nahi ho raha hai.")
        seconds = max(0, seconds)

        old_source = track.source
        old_was_url = track.is_url

        seeked_path = await _cut_from(old_source, seconds)
        new_track = Track(
            source=seeked_path,
            title=track.title,
            requested_by=track.requested_by,
            is_url=False,
        )
        try:
            await _start_stream(chat_id, new_track)
        except Exception:
            # The cut file is useless if we couldn't switch to it.
            if os.path.exists(seeked_path):
                try:
                    os.remove(seeked_path)
                except OSError:
                    pass
            raise

        # started_at is shifted back so /vnowplaying keeps reporting an
        # accurate elapsed time after the seek.
        state.started_at = time.time() - seconds
        state.paused_duration = 0.0

        if not old_was_url and old_source != seeked_path and os.path.exists(old_source):
            try:
                os.remove(old_source)
            except OSError:
                pass


async def _cut_from(source: str, offset_seconds: int) -> str:
    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)
    out_path = os.path.join(config.DOWNLOADS_DIR, f"seek_{uuid.uuid4().hex}.mp4")

    # Fast path: stream-copy (no re-encoding). "-ss" before "-i" seeks to
    # the nearest keyframe cheaply, which is what makes this feel instant.
    fast_cmd = [
        "ffmpeg", "-y",
        "-ss", str(offset_seconds),
        "-i", source,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        out_path,
    ]
    ok = await _run_ffmpeg(fast_cmd)

    if not ok or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        # Fallback: re-encode. Slower, but works even when the source
        # can't be seeked with a plain stream-copy.
        slow_cmd = [
            "ffmpeg", "-y",
            "-ss", str(offset_seconds),
            "-i", source,
            "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac",
            out_path,
        ]
        ok = await _run_ffmpeg(slow_cmd)
        if not ok or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            raise RuntimeError(
                "Seek fail ho gaya — is media source par seek possible nahi lag raha. "
                "Ho sakta hai requested time media ki length se zyada ho."
            )
    return out_path


async def _run_ffmpeg(cmd: list) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "ffmpeg install nahi hai — seek ke liye ffmpeg chahiye. "
            "(Heroku par Aptfile + apt buildpack check karein.)"
        ) from e
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.warning(
            "ffmpeg failed (rc=%s): %s",
            process.returncode,
            (stderr or b"").decode(errors="ignore")[-500:],
        )
        return False
    return True


def _cleanup_file(track: Optional[Track]) -> None:
    if track and not track.is_url and track.source and os.path.exists(track.source):
        try:
            os.remove(track.source)
        except OSError:
            pass


# --- Auto-advance when a track finishes --------------------------------
# Without this the queue never moves on by itself: the stream just ends and
# the bot sits in the call doing nothing until someone runs /vskip.


async def _on_stream_end(_client, update) -> None:
    chat_id = getattr(update, "chat_id", None)
    if chat_id is None:
        return
    try:
        async with _lock(chat_id):
            next_track = await _advance(chat_id)
    except Exception:  # noqa: BLE001 - a background handler must never die
        logger.exception("Auto-advance failed for chat %s", chat_id)
        return

    from bot.core.clients import bot as bot_client

    try:
        if next_track:
            await bot_client.send_message(chat_id, f"▶️ Ab chal raha hai: {next_track.title}")
        else:
            await bot_client.send_message(chat_id, "✅ Queue khatam — video chat se nikal gaya.")
    except Exception:  # noqa: BLE001 - bot may not be able to post there
        pass


def _register_stream_end_handler() -> None:
    # PyTgCalls moved this between `@on_stream_end()` (1.x / early 2.x) and
    # `@on_update(filters.stream_end)` (2.1+). Try both, quietly.
    try:
        from pytgcalls import filters as pytgcalls_filters

        flt = getattr(pytgcalls_filters, "stream_end", None)
        if flt is not None:
            if callable(flt):
                try:
                    flt = flt()
                except TypeError:
                    pass
            call_py.on_update(flt)(_on_stream_end)
            logger.debug("Registered stream_end via on_update filter")
            return
    except Exception:  # noqa: BLE001
        pass

    try:
        call_py.on_stream_end()(_on_stream_end)
        logger.debug("Registered stream_end via on_stream_end decorator")
        return
    except Exception:  # noqa: BLE001
        pass

    logger.warning(
        "Stream-end handler register nahi ho paya — queue automatically "
        "aage nahi badhegi, /vskip manually use karna padega."
    )


_register_stream_end_handler()
