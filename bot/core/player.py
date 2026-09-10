"""Wraps PyTgCalls so the command handlers stay simple. Also implements
seeking, which PyTgCalls itself does not expose directly: we cut a fresh
local file starting at the requested offset with ffmpeg (fast stream-copy
first, falling back to a re-encode if the source can't be copy-seeked),
then hand that file to PyTgCalls like any other track.
"""
import asyncio
import os
import time
import uuid
from typing import Optional

from pytgcalls.types import AudioQuality, MediaStream, VideoQuality

from bot import config
from bot.core.clients import call_py
from bot.core.queue import Track, clear_state, get_state
from bot.logger import logger

_VIDEO_QUALITY = {
    "sd_480p": "SD_480p",
    "hd_720p": "HD_720p",
    "fhd_1080p": "FHD_1080p",
}


def _video_quality():
    attr = _VIDEO_QUALITY.get(config.DEFAULT_VIDEO_QUALITY, "SD_480p")
    return getattr(VideoQuality, attr, VideoQuality.SD_480p)


def _audio_quality():
    if config.DEFAULT_AUDIO_QUALITY == "low":
        return AudioQuality.LOW
    if config.DEFAULT_AUDIO_QUALITY == "medium":
        return AudioQuality.MEDIUM
    return AudioQuality.HIGH


async def _safe_call(method_name: str, *args, friendly_action: str = ""):
    """Calls call_py.<method_name>(*args), with a clear error if this
    PyTgCalls version doesn't expose that method under this name (library
    APIs shift between versions — this fails loudly and usefully instead
    of crashing with a bare AttributeError).
    """
    method = getattr(call_py, method_name, None)
    if method is None:
        raise RuntimeError(
            f"PyTgCalls me `{method_name}` method nahi mila (action: "
            f"{friendly_action or method_name}). Installed `py-tgcalls` "
            f"version check karein — requirements.txt me version badalna "
            f"pad sakta hai."
        )
    return await method(*args)


async def _start_stream(chat_id: int, track: Track, is_update: bool = False) -> None:
    stream = MediaStream(track.source, _audio_quality(), _video_quality())
    method_name = "change_stream" if is_update else "play"
    try:
        await _safe_call(method_name, chat_id, stream, friendly_action="playback start/update")
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001 - genuinely need the broad catch here
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


async def play_or_queue(chat_id: int, track: Track) -> bool:
    """Starts playing immediately if nothing is playing yet, otherwise adds
    to the queue. Returns True if playback started immediately.
    """
    state = get_state(chat_id)
    if state.current is None:
        await _start_stream(chat_id, track, is_update=False)
        return True
    state.queue.append(track)
    return False


async def skip(chat_id: int) -> Optional[Track]:
    state = get_state(chat_id)
    _cleanup_file(state.current)
    if not state.queue:
        try:
            await _safe_call("leave_call", chat_id, friendly_action="leave call")
        except Exception:
            pass  # best-effort — we're clearing local state regardless
        clear_state(chat_id)
        return None
    next_track = state.queue.pop(0)
    await _start_stream(chat_id, next_track, is_update=True)
    return next_track


async def stop(chat_id: int) -> None:
    state = get_state(chat_id)
    _cleanup_file(state.current)
    for track in state.queue:
        _cleanup_file(track)
    try:
        await _safe_call("leave_call", chat_id, friendly_action="leave call")
    except Exception:
        pass  # best-effort
    clear_state(chat_id)


async def pause(chat_id: int) -> None:
    await _safe_call("pause", chat_id, friendly_action="pause")
    state = get_state(chat_id)
    state.is_paused = True
    state.paused_at = time.time()


async def resume(chat_id: int) -> None:
    await _safe_call("resume", chat_id, friendly_action="resume")
    state = get_state(chat_id)
    if state.is_paused and state.paused_at:
        state.paused_duration += time.time() - state.paused_at
    state.is_paused = False
    state.paused_at = None


async def mute(chat_id: int) -> None:
    await _safe_call("mute", chat_id, friendly_action="mute")


async def unmute(chat_id: int) -> None:
    await _safe_call("unmute", chat_id, friendly_action="unmute")


async def seek(chat_id: int, seconds: int) -> None:
    state = get_state(chat_id)
    track = state.current
    if track is None:
        raise RuntimeError("Abhi kuch play nahi ho raha hai.")
    seconds = max(0, seconds)

    seeked_path = await _cut_from(track.source, seconds)
    new_track = Track(
        source=seeked_path,
        title=track.title,
        requested_by=track.requested_by,
        is_url=False,
    )
    old_source = track.source
    old_was_url = track.is_url
    await _start_stream(chat_id, new_track, is_update=True)

    # started_at is shifted back so /vnowplaying keeps reporting an
    # accurate elapsed time after the seek.
    state.started_at = time.time() - seconds
    state.paused_duration = 0.0

    if not old_was_url and os.path.exists(old_source):
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
        if not ok:
            raise RuntimeError(
                "Seek fail ho gaya — is media source par seek possible nahi lag raha."
            )
    return out_path


async def _run_ffmpeg(cmd: list) -> bool:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.warning("ffmpeg failed (%s): %s", cmd[0:2], stderr.decode(errors="ignore")[-500:])
        return False
    return True


def _cleanup_file(track: Optional[Track]) -> None:
    if track and not track.is_url and track.source and os.path.exists(track.source):
        try:
            os.remove(track.source)
        except OSError:
            pass
