"""Wraps PyTgCalls so the command handlers stay simple.

Root-cause notes (v1.3.0):

* The old "Video/voice chat se connect nahi ho paya ... (Technical: )" error
  had an EMPTY technical part because some exceptions (asyncio.TimeoutError,
  several ntgcalls errors) have no message. We now always report the
  exception *type* and translate the known ones into a precise reason.
* Before joining we now check whether the group actually has a video chat
  running. If not, the assistant starts one itself (needs "Manage Video
  Chats"); otherwise the user gets a clear "video chat start karein".
* A stale/dead PyTgCalls connection for the chat (e.g. the video chat was
  ended manually) made every later /vplay fail. We now leave the stale call
  and retry once with a fresh join.
* PyTgCalls 2.x fires StreamEnded once for AUDIO *and* once for VIDEO. The
  old handler advanced the queue on both, so every video skipped the next
  track instantly. A per-chat `generation` counter now makes each track end
  exactly once.

v1.4.0:

* The duplicate-StreamEnded guard only worked for tracks WITHOUT a known
  duration, so Telegram videos (which have one) could still skip the next
  track when the audio/video end events arrived a second apart. Every end
  event within a few seconds of a (re)start is now ignored for every track.
* Our own "leave + rejoin" retry fired the call-closed handler, which wiped
  the whole queue/state mid-play. Intentional leaves are now ignored there.
* Pause/mute state is kept in sync when the track changes (skip while
  paused used to leave the new track silently paused).
* pause/resume/mute/unmute now take the per-chat lock, so they can't race a
  skip/seek/auto-advance.
"""
import asyncio
import os
import random
import time
import uuid
from typing import Optional

from pytgcalls.types import AudioQuality, MediaStream, VideoQuality

# py-tgcalls 2.x exports AudioParameters / VideoParameters from
# `pytgcalls.types.raw` (NOT `pytgcalls.types`). The old import always
# failed silently, so the bot never used its own quality settings and fell
# back to enum defaults + a broken "-vf:video:mid" flag that py-tgcalls
# simply dropped.
try:
    from pytgcalls.types.raw import AudioParameters, VideoParameters
except ImportError:  # pragma: no cover - very old builds
    try:
        from pytgcalls.types import AudioParameters, VideoParameters
    except ImportError:
        AudioParameters = VideoParameters = None

from bot import config
from bot.core.clients import assistant, bot, call_py
from bot.core.queue import Track, clear_state, get_state
from bot.logger import logger

# (width, height). Telegram renders group-call video at max 720p; 1080p
# only costs CPU and makes WebRTC drop bitrate on small servers.
_VIDEO_SIZES = {
    "sd_360p": (640, 360),
    "sd_480p": (854, 480),
    "hd_720p": (1280, 720),
    "hd_1080p": (1920, 1080),
    "fhd_1080p": (1920, 1080),
}

# Method-name aliases across PyTgCalls versions, most recent name first.
_METHOD_ALIASES = {
    "play": ("play", "join_group_call", "change_stream"),
    "leave": ("leave_call", "leave_group_call"),
    "pause": ("pause", "pause_stream"),
    "resume": ("resume", "resume_stream"),
    "mute": ("mute", "mute_stream"),
    "unmute": ("unmute", "unmute_stream"),
    "change_volume": ("change_volume_call", "change_volume"),
}

_locks: dict = {}
# Chats we are leaving on purpose (stop / retry) — the call-closed handler
# must not treat those as "video chat ended by someone".
_expected_leave: set = set()
# Ignore "stream ended" events this soon after a stream (re)starts.
_END_GUARD_SECONDS = 4.0

# Callback set by the plugin layer to post the "now playing" card when the
# queue auto-advances. Signature: async (chat_id, track_or_None, reason="ended") -> None
on_track_change = None


def _lock(chat_id: int) -> asyncio.Lock:
    lock = _locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[chat_id] = lock
    return lock


def _fps() -> int:
    return min(max(int(getattr(config, "VIDEO_FPS", 30) or 30), 15), 30)


def _video_quality():
    """Target size keeps the source aspect ratio (py-tgcalls computes the
    real height from the width; adjust_by_height caps portrait videos so a
    1080x1920 clip is not encoded at full size)."""
    key = (getattr(config, "DEFAULT_VIDEO_QUALITY", "hd_720p") or "hd_720p").lower()
    w, h = _VIDEO_SIZES.get(key, (1280, 720))
    if VideoParameters is not None:
        try:
            return VideoParameters(width=w, height=h, frame_rate=_fps(), adjust_by_height=True)
        except (TypeError, ValueError) as e:
            logger.warning("VideoParameters rejected (%s) — enum fallback", e)
    name = {640: "SD_360p", 854: "SD_480p", 1280: "HD_720p", 1920: "FHD_1080p"}.get(w, "HD_720p")
    return getattr(VideoQuality, name, VideoQuality.HD_720p)


def _audio_quality():
    """48 kHz, stereo by default (AUDIO_CHANNELS=1 for mono)."""
    lvl = (getattr(config, "DEFAULT_AUDIO_QUALITY", "high") or "high").lower()
    if lvl == "low":
        return AudioQuality.LOW
    if lvl == "medium":
        return AudioQuality.MEDIUM
    if AudioParameters is not None:
        try:
            ch = 1 if int(getattr(config, "AUDIO_CHANNELS", 2) or 2) == 1 else 2
            return AudioParameters(bitrate=48000, channels=ch)
        except (TypeError, ValueError) as e:
            logger.warning("AudioParameters rejected (%s) — enum fallback", e)
    return AudioQuality.HIGH


def _readrate_flag() -> str:
    """Optional read-rate cap. EMPTY BY DEFAULT — ffmpeg's output pipe already
    gives natural back-pressure; -readrate >1 made both ffmpeg processes pull
    2x realtime and starve each other. Override: FFMPEG_READRATE=1.1..1.3"""
    try:
        val = float(getattr(config, "FFMPEG_READRATE", 0) or 0)
    except (TypeError, ValueError):
        val = 0.0
    return f"-readrate {val:g}" if val > 0 else ""


def _ffmpeg_params(offset: int = 0) -> str:
    """Tier 1 — buffered input + audio drift correction.

    py-tgcalls syntax: sections `--audio` / `--video` (default: both) and
    positions `-b:start` (before -i), `-b:mid` (after -i) and `-b:end`
    (just before the output). The old "-vf:video:mid" style was not valid
    syntax and was silently dropped.
    """
    threads = max(1, int(getattr(config, "FFMPEG_THREADS", 2) or 2))
    probe = int(getattr(config, "FFMPEG_PROBE_SIZE", 3000000) or 3000000)
    analyze = int(getattr(config, "FFMPEG_ANALYZE_US", 3000000) or 3000000)
    rw_timeout = int(getattr(config, "FFMPEG_RW_TIMEOUT_US", 15000000) or 15000000)
    parts = [
        "-b:start",
        "-thread_queue_size", "4096",
        "-probesize", str(probe),
        "-analyzeduration", str(analyze),
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-rw_timeout", str(rw_timeout),
        "-threads", str(threads),
    ]
    rr = _readrate_flag()
    if rr:
        parts += rr.split()
    if offset > 0:
        parts += ["-ss", str(int(offset))]
    # Audio only: resample with drift compensation so A/V stay in sync after
    # a network hiccup instead of crackling.
    parts += ["--audio", "-b:end", "-af", "aresample=async=1000:first_pts=0"]
    return " ".join(parts)


def _ffmpeg_params_safe(offset: int = 0) -> str:
    """Tier 2 — buffering only, no filters."""
    threads = max(1, int(getattr(config, "FFMPEG_THREADS", 2) or 2))
    parts = [
        "-b:start",
        "-thread_queue_size", "1024",
        "-probesize", "3000000",
        "-analyzeduration", "3000000",
        "-fflags", "+genpts+discardcorrupt",
        "-threads", str(threads),
    ]
    if offset > 0:
        parts += ["-ss", str(int(offset))]
    return " ".join(parts)


def _ffmpeg_params_minimal(offset: int = 0) -> str:
    """Tier 3 — last resort: plain input seek only."""
    return f"-b:start -ss {int(offset)}" if offset > 0 else ""


async def preflight_probe(source: str) -> None:
    """Runs ffprobe ourselves BEFORE handing the source to PyTgCalls.

    PyTgCalls' own ffprobe check calls `kill()` on an ffprobe that has
    already exited when the output is empty, which surfaces as a useless
    `ProcessLookupError`. Probing first gives the real ffprobe error (bad
    link, 403/404, streamer could not read the Telegram file, ffmpeg
    missing) as a clear message.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json", "-show_format",
            "-show_streams",
            "-probesize", "2000000", "-analyzeduration", "2000000",
            source,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "⚠️ Server par ffmpeg/ffprobe install nahi hai. Heroku par "
            "'heroku-community/apt' buildpack add karein (Aptfile me ffmpeg hai), phir redeploy."
        ) from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(
            "⚠️ Media 20 second me read nahi hui (file/link bahut slow hai). Dobara try karein."
        ) from e
    try:
        import json

        data = json.loads(out.decode("utf-8", "ignore") or "{}")
    except ValueError:
        data = {}
    if not data.get("streams"):
        reason = (err.decode("utf-8", "ignore").strip().splitlines() or ["koi stream nahi mili"])[-1]
        raise RuntimeError(
            "⚠️ Media read nahi ho payi — file/link sahi nahi hai ya access nahi mila.\n"
            f"(ffprobe: {reason[:200]})"
        )


def describe_error(e: BaseException) -> str:
    """Never-empty description of an exception: 'Type: message' or 'Type'."""
    name = type(e).__name__
    msg = str(e).strip()
    return f"{name}: {msg}" if msg else name


async def _leave(chat_id: int) -> None:
    """Leaves the call on purpose, without triggering the call-closed reset."""
    _expected_leave.add(chat_id)
    try:
        await _safe_call("leave", chat_id)
    except Exception:  # noqa: BLE001 - best-effort
        pass
    finally:
        asyncio.get_event_loop().call_later(3, _expected_leave.discard, chat_id)


async def _safe_call(action: str, *args):
    """Calls the first PyTgCalls method matching `action`'s known aliases."""
    for name in _METHOD_ALIASES.get(action, (action,)):
        method = getattr(call_py, name, None)
        if callable(method):
            return await method(*args)
    raise RuntimeError(
        f"PyTgCalls me `{action}` ke liye koi known method nahi mila. "
        "Installed `py-tgcalls` version requirements.txt se match nahi kar raha."
    )


# --- Group call checks --------------------------------------------------


async def _resolve_assistant_peer(chat_id: int):
    try:
        return await assistant.resolve_peer(chat_id)
    except Exception:  # noqa: BLE001 - unknown peer after restart
        await warm_up_assistant_peers()
        return await assistant.resolve_peer(chat_id)


async def ensure_group_call(chat_id: int) -> None:
    """Makes sure the group has a running video chat, starting one with the
    assistant if needed. Raises RuntimeError with a clear reason otherwise.
    """
    from pyrogram import raw

    try:
        peer = await _resolve_assistant_peer(chat_id)
        if isinstance(peer, raw.types.InputPeerChannel):
            full = await assistant.invoke(
                raw.functions.channels.GetFullChannel(
                    channel=raw.types.InputChannel(
                        channel_id=peer.channel_id, access_hash=peer.access_hash
                    )
                )
            )
        elif isinstance(peer, raw.types.InputPeerChat):
            full = await assistant.invoke(raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
        else:
            return
    except Exception as e:  # noqa: BLE001 - let play() try anyway
        logger.warning("Chat %s: group call check fail (%s)", chat_id, describe_error(e))
        return

    if getattr(full.full_chat, "call", None) is not None:
        return

    logger.info("Chat %s: video chat band hai — assistant se start kar raha hoon", chat_id)
    try:
        await assistant.invoke(
            raw.functions.phone.CreateGroupCall(peer=peer, random_id=random.randint(1, 2**31 - 1))
        )
        await asyncio.sleep(1.5)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Group me video chat ON nahi hai, aur assistant use khud start nahi kar paya.\n"
            "👉 Group me video chat start karein, ya assistant ko admin banakar "
            "'Manage Video Chats' permission dein.\n"
            f"(Technical: {describe_error(e)})"
        ) from e


def _translate_play_error(e: BaseException) -> str:
    name = type(e).__name__
    low = f"{name} {e}".lower()
    if "noactivegroupcall" in low or "no active group call" in low:
        return "Group me video chat ON nahi hai. Pehle video chat start karein."
    if "chatadminrequired" in low or "chat_admin_required" in low or "groupcall_forbidden" in low:
        return "Assistant ko admin banayein aur 'Manage Video Chats' permission dein."
    if "useralreadyparticipant" in low or "groupcall_already" in low:
        return "Assistant pehle se kisi aur call me atka hai. /vstop bhejkar dobara try karein."
    if "timeout" in low or "timedout" in low:
        return ("Video chat se connect hone me time out ho gaya. Video chat band karke "
                "dobara start karein, phir /vplay bhejein.")
    if "processlookuperror" in low or "jsondecodeerror" in low:
        return ("Media file read nahi ho payi (ffprobe ko data nahi mila). File/link check "
                "karein aur dobara /vplay bhejein.")
    if "ffmpeg" in low or "filenotfound" in low or "fileerror" in low or "nosource" in low \
            or "noaudiosource" in low or "novideosource" in low:
        return ("Media file read nahi ho payi (ffmpeg). File corrupt ho sakti hai, ya server "
                "par ffmpeg install nahi hai (Heroku: apt buildpack + Aptfile).")
    if "peer_id_invalid" in low or "peeridinvalid" in low:
        return "Assistant ye group resolve nahi kar paya. Assistant ko group me add karein."
    if "connection" in low or "telegramserver" in low or "rtmp" in low:
        return "Telegram call server se connection nahi bana. Thodi der baad dobara try karein."
    # --- PyTgCalls-specific exceptions ---
    if "novideosourcefound" in low:
        return "File me video track nahi hai. Audio-only file ke liye normal /vplay try karein."
    if "noaudiosourcefound" in low:
        return "File me audio track nahi hai — bot stream nahi kar sakta."
    if "imagesourcefound" in low:
        return "Ye image file hai, video/audio nahi. Sahi file bhejein."
    if "livestreamfound" in low:
        return "Live stream supported nahi hai. Recording file bhejein."
    if "invalidvideoproportion" in low:
        return "Video ka aspect ratio valid nahi (bahut chhota ya bahut bada)."
    return ("Video/voice chat se connect nahi ho paya. Check karein ki video chat ON hai aur "
            "assistant admin hai (Manage Video Chats).")


async def _start_stream(chat_id: int, track: Track, offset: int = 0) -> None:
    def _mk_stream(params: str):
        if params:
            return MediaStream(
                track.source, _audio_quality(), _video_quality(),
                ffmpeg_parameters=params,
            )
        return MediaStream(track.source, _audio_quality(), _video_quality())

    # Richest first, minimal last. If FFmpeg rejects a tier at play() time we
    # step down one tier and retry instead of failing the whole /vplay.
    _tiers = (
        _ffmpeg_params(offset),          # 1: buffer + audio gap-fill
        _ffmpeg_params_safe(offset),     # 2: buffer only
        _ffmpeg_params_minimal(offset),  # 3: -ss only
    )
    _tier = 1
    try:
        stream = _mk_stream(_tiers[0])
    except TypeError:
        # Very old build: no ffmpeg_parameters kwarg at all.
        stream = MediaStream(track.source, _audio_quality(), _video_quality())
    except Exception as e:  # noqa: BLE001
        logger.warning("rich ffmpeg params rejected (%s) — safe params", e)
        _tier = 2
        stream = _mk_stream(_tiers[1])


    state = get_state(chat_id)
    is_switch = state.current is not None  # already in the call -> just switch source
    was_paused = state.is_paused
    await preflight_probe(track.source)
    if not is_switch:
        await ensure_group_call(chat_id)

    logger.info("Chat %s: stream start — %s (offset %s)", chat_id, track.title, offset)
    _vq, _aq = _video_quality(), _audio_quality()
    logger.info(
        "QUALITY — video=%s audio=%s",
        f"{getattr(_vq, 'width', '?')}x{getattr(_vq, 'height', '?')}@{getattr(_vq, 'frame_rate', '?')}"
        if VideoParameters is not None and isinstance(_vq, VideoParameters) else _vq,
        f"{getattr(_aq, 'bitrate', '?')}Hz/{getattr(_aq, 'channels', '?')}ch"
        if AudioParameters is not None and isinstance(_aq, AudioParameters) else _aq,
    )
    # SEEK/SKIP FIX: PyTgCalls fires StreamEnded for the OLD stream when
    # play() replaces it. _on_stream_end uses state.last_start as a guard
    # to ignore this stale event — but seek()/skip() never updated it,
    # so the guard never fired and the queue auto-advanced (=stream hat jati thi).
    state.last_start = time.time()
    last_error: Optional[BaseException] = None
    for attempt in (1, 2):
        try:
            await _safe_call("play", chat_id, stream)
            last_error = None
            break
        except RuntimeError as e:
            if "known method" in str(e):
                raise
            last_error = e
        except Exception as e:  # noqa: BLE001
            last_error = e
            if type(e).__name__ in ("ProcessLookupError", "JSONDecodeError"):
                await asyncio.sleep(1)  # transient ffprobe read failure — just retry
                if attempt == 1:
                    logger.warning("Chat %s: ffprobe read fail, retrying", chat_id)
                    continue
        logger.warning("Chat %s: play attempt %s failed — %s", chat_id, attempt, describe_error(last_error))
        if attempt == 1:
            # If FFmpeg rejected the rich parameter set, step down one tier so
            # the retry actually has a chance.
            if _tier < 3:
                _tier += 1
                try:
                    stream = _mk_stream(_tiers[_tier - 1])
                    logger.info("Chat %s: ffmpeg param tier -> %s/3", chat_id, _tier)
                except Exception as _te:  # noqa: BLE001
                    logger.debug("tier rebuild fail: %s", _te)
            # Drop a stale/dead connection and retry with a fresh join.
            await _leave(chat_id)
            await asyncio.sleep(1.5)
            try:
                await ensure_group_call(chat_id)
            except RuntimeError:
                pass

    if last_error is not None:
        raise RuntimeError(
            f"{_translate_play_error(last_error)}\n(Technical: {describe_error(last_error)})"
        ) from last_error

    # We're in the call again: from now on a "call closed" event is real.
    _expected_leave.discard(chat_id)

    state.current = track
    state.started_at = time.time() - offset
    state.last_start = time.time()
    state.paused_duration = 0.0
    state.is_paused = False
    state.paused_at = None
    state.generation += 1

    # The call keeps its own pause/mute flags across a source switch. A new
    # stream must play (callers that want it paused, e.g. seek, re-pause),
    # and a muted call stays muted so the button label stays truthful.
    if was_paused:
        try:
            await _safe_call("resume", chat_id)
        except Exception:  # noqa: BLE001 - not paused at call level = fine
            pass
    if state.is_muted:
        try:
            await _safe_call("mute", chat_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("re-mute fail: %s", e)
            state.is_muted = False


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
    from pyrogram.enums import ChatMemberStatus
    from pyrogram.errors import UserAlreadyParticipant, UserNotParticipant

    async def _is_member() -> bool:
        m = await assistant.get_chat_member(chat_id, "me")
        return m.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)

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

    # A banned assistant can't join via link — unban it first if possible.
    try:
        from bot.core import runtime
        if runtime.ASSISTANT_ID:
            await bot.unban_chat_member(chat_id, runtime.ASSISTANT_ID)
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
        raise RuntimeError(f"Assistant group join nahi kar paya: {describe_error(e)}") from e
    await asyncio.sleep(1)
    await warm_up_assistant_peers()


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
    finished = state.current

    # --- Loop TRACK: current track replay ---
    if state.loop_mode == "track" and finished is not None:
        try:
            state.current = None
            await _start_stream(chat_id, finished)
            return finished
        except Exception as e:  # noqa: BLE001
            logger.warning("Loop-track replay fail: %s", describe_error(e))
            # fail hua -> normal flow me continue (agla track try karega)

    # --- Loop QUEUE: current track ko end me wapas daalo ---
    _requeue = state.loop_mode == "queue" and finished is not None
    if _requeue:
        state.queue.append(finished)

    # Cleanup sirf tab jab track queue me wapas nahi ja raha
    if not _requeue:
        _cleanup_file(finished)
    state.current = None

    while state.queue:
        next_track = state.queue.pop(0)
        try:
            await _start_stream(chat_id, next_track)
            return next_track
        except Exception as e:  # noqa: BLE001 - skip a broken track, keep going
            logger.warning("Track '%s' play nahi ho paya, skipping: %s", next_track.title, e)
            _cleanup_file(next_track)

    await _leave(chat_id)
    clear_state(chat_id)
    return None


async def skip(chat_id: int) -> Optional[Track]:
    async with _lock(chat_id):
        state = get_state(chat_id)
        if state.current is None:
            raise RuntimeError("Abhi kuch play nahi ho raha hai.")
        state.loop_mode = "off"
        return await _advance(chat_id)


async def stop(chat_id: int) -> None:
    async with _lock(chat_id):
        state = get_state(chat_id)
        _cleanup_file(state.current)
        for track in state.queue:
            _cleanup_file(track)
        await _leave(chat_id)
        clear_state(chat_id)


async def leave_all() -> None:
    """Shutdown helper: leave every active call so the assistant doesn't
    stay stuck as a silent participant after a restart."""
    from bot.core.queue import _states

    for chat_id in list(_states):
        try:
            await stop(chat_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("leave_all %s: %s", chat_id, e)


async def pause(chat_id: int) -> None:
    async with _lock(chat_id):
        state = get_state(chat_id)
        if state.current is None:
            raise RuntimeError("Abhi kuch play nahi ho raha hai.")
        if state.is_paused:
            raise RuntimeError("Playback pehle se hi paused hai.")
        await _safe_call("pause", chat_id)
        state.is_paused = True
        state.paused_at = time.time()


async def resume(chat_id: int) -> None:
    async with _lock(chat_id):
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
    async with _lock(chat_id):
        state = get_state(chat_id)
        if state.current is None:
            raise RuntimeError("Abhi kuch play nahi ho raha hai.")
        await _safe_call("mute", chat_id)
        state.is_muted = True


async def unmute(chat_id: int) -> None:
    async with _lock(chat_id):
        state = get_state(chat_id)
        if state.current is None:
            raise RuntimeError("Abhi kuch play nahi ho raha hai.")
        await _safe_call("unmute", chat_id)
        state.is_muted = False


def cycle_loop_mode(chat_id: int) -> str:
    """Cycle: off -> track -> queue -> off. Returns new mode."""
    state = get_state(chat_id)
    if state.current is None:
        raise RuntimeError("Abhi kuch play nahi ho raha hai.")
    _cycle = {"off": "track", "track": "queue", "queue": "off"}
    state.loop_mode = _cycle.get(state.loop_mode, "off")
    logger.info("Chat %s: loop_mode -> %s", chat_id, state.loop_mode)
    return state.loop_mode

# Backward-compat shim (agar koi purana code toggle_loop call kare)

async def set_volume(chat_id: int, volume: int) -> int:
    """Set playback volume (1-200). Returns applied volume."""
    async with _lock(chat_id):
        state = get_state(chat_id)
        if state.current is None:
            raise RuntimeError("Abhi kuch play nahi ho raha hai.")
        volume = max(1, min(200, int(volume)))
        # PyTgCalls 2.x: change_volume_call(chat_id, volume)
        await _safe_call("change_volume", chat_id, volume)
        state.volume = volume
        return volume

def toggle_loop(chat_id: int) -> bool:
    mode = cycle_loop_mode(chat_id)
    return mode != "off"


def shuffle(chat_id: int) -> int:
    state = get_state(chat_id)
    if len(state.queue) < 2:
        raise RuntimeError("Shuffle ke liye queue me kam se kam 2 tracks chahiye.")
    random.shuffle(state.queue)
    return len(state.queue)


async def seek(chat_id: int, seconds: int) -> int:
    """Seeks to an absolute position. Returns the position actually used."""
    async with _lock(chat_id):
        state = get_state(chat_id)
        track = state.current
        if track is None:
            raise RuntimeError("Abhi kuch play nahi ho raha hai.")
        seconds = max(0, int(seconds))
        if track.duration and seconds >= track.duration - 2:
            raise RuntimeError(
                f"Media sirf {track.duration // 60}:{track.duration % 60:02d} lamba hai — "
                "itna aage seek nahi ho sakta."
            )
        was_paused = state.is_paused
        # Local file + URL dono ke liye ek hi raasta: ffmpeg_parameters="-ss N"
        # (pehle local file ke liye _cut_from se poori file cut hoti thi — slow
        #  aur temp files bhi banti thi. Ab instant seek.)
        await _start_stream(chat_id, track, offset=seconds)
        state.started_at = time.time() - seconds
        if was_paused:
            try:
                await _safe_call("pause", chat_id)
                state.is_paused = True
                state.paused_at = time.time()
            except Exception:  # noqa: BLE001
                pass
        return seconds


async def seek_relative(chat_id: int, delta: int) -> int:
    state = get_state(chat_id)
    if state.current is None:
        raise RuntimeError("Abhi kuch play nahi ho raha hai.")
    return await seek(chat_id, max(0, state.elapsed() + delta))


async def _cut_from(source: str, offset_seconds: int) -> str:
    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)
    out_path = os.path.join(config.DOWNLOADS_DIR, f"seek_{uuid.uuid4().hex}.mp4")

    fast_cmd = [
        "ffmpeg", "-y", "-ss", str(offset_seconds), "-i", source,
        "-c", "copy", "-avoid_negative_ts", "make_zero", out_path,
    ]
    ok = await _run_ffmpeg(fast_cmd)

    if not ok or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        slow_cmd = [
            "ffmpeg", "-y", "-ss", str(offset_seconds), "-i", source,
            "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", out_path,
        ]
        ok = await _run_ffmpeg(slow_cmd)
        if not ok or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            _remove(out_path)
            raise RuntimeError(
                "Seek fail ho gaya — ho sakta hai requested time media ki length se zyada ho."
            )
    return out_path


async def _run_ffmpeg(cmd: list) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "ffmpeg install nahi hai. (Heroku par Aptfile + apt buildpack check karein.)"
        ) from e
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.warning("ffmpeg failed (rc=%s): %s", process.returncode,
                       (stderr or b"").decode(errors="ignore")[-500:])
        return False
    return True


def _remove(path: Optional[str]) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _cleanup_file(track: Optional[Track]) -> None:
    if track and not track.is_url:
        _remove(track.source)


# --- Auto-advance when a track finishes --------------------------------


async def _on_stream_end(_client, update) -> None:
    chat_id = getattr(update, "chat_id", None)
    if chat_id is None:
        return
    state = get_state(chat_id)
    if state.current is None:
        return
    gen_at_event = state.generation
    try:
        async with _lock(chat_id):
            state = get_state(chat_id)
            # PyTgCalls sends one event for audio AND one for video. If the
            # track already changed while we waited for the lock, ignore.
            if state.current is None or state.generation != gen_at_event:
                return
            # Ignore duplicate / stale end events right after a (re)start,
            # skip or seek — for EVERY track (Telegram files have a duration
            # too, and those used to double-skip). Very short clips get a
            # proportionally shorter guard so they still advance.
            guard = _END_GUARD_SECONDS
            if state.current.duration:
                guard = min(guard, max(1.0, state.current.duration / 2))
            if time.time() - state.last_start < guard:
                return
            next_track = await _advance(chat_id)
    except Exception:  # noqa: BLE001 - a background handler must never die
        logger.exception("Auto-advance failed for chat %s", chat_id)
        return

    if on_track_change is not None:
        try:
            await on_track_change(chat_id, next_track)
        except Exception:  # noqa: BLE001
            logger.exception("Now-playing card post fail (chat %s)", chat_id)


async def _on_call_closed(_client, update) -> None:
    """Video chat ended / assistant kicked — clear state so the next /vplay
    starts clean instead of trying to reuse a dead call."""
    chat_id = getattr(update, "chat_id", None)
    if chat_id is None:
        return
    if chat_id in _expected_leave or _lock(chat_id).locked():
        return  # our own stop/retry, or a (re)start in progress
    state = get_state(chat_id)
    if state.current is None:
        return
    logger.info("Chat %s: call band ho gayi — state clear", chat_id)
    for track in [state.current, *state.queue]:
        _cleanup_file(track)
    clear_state(chat_id)
    if on_track_change is not None:
        try:
            await on_track_change(chat_id, None, reason="closed")
        except Exception:  # noqa: BLE001
            logger.exception("Call-closed notice fail (chat %s)", chat_id)


def _register_handlers() -> None:
    try:
        from pytgcalls import filters as fl

        stream_end = getattr(fl, "stream_end", None)
        if stream_end is not None:
            call_py.on_update(stream_end() if isinstance(stream_end, type) else stream_end)(_on_stream_end)
            chat_update = getattr(fl, "chat_update", None)
            if chat_update is not None:
                try:
                    from pytgcalls.types import ChatUpdate
                    call_py.on_update(chat_update(ChatUpdate.Status.LEFT_CALL))(_on_call_closed)
                except Exception as e:  # noqa: BLE001
                    logger.debug("chat_update register fail: %s", e)
            return
    except Exception as e:  # noqa: BLE001
        logger.debug("on_update register fail: %s", e)

    try:
        call_py.on_stream_end()(_on_stream_end)
        return
    except Exception:  # noqa: BLE001
        pass

    logger.warning("Stream-end handler register nahi ho paya — /vskip manually use karna padega.")


_register_handlers()
