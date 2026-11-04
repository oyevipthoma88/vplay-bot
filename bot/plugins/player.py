import asyncio
import html
import os

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Message

from bot import config
from bot.core import player as player_core
from bot.core.clients import assistant, bot
from bot.core.logger_gc import log_play, log_stop
from bot.core.queue import get_state
from bot.database import add_served_chat, add_served_user, increment_play_count
from bot.logger import logger
from bot.utils import ui
from bot.utils.commands import cmd
from bot.utils.decorators import admin_only, error_guard
from bot.utils.extract import resolve_track
from bot.utils.formatting import format_duration, parse_time_to_seconds

_GROUP = filters.group
_PRIVATE = filters.private

_PRIVATE_HINT = (
    "Ye command sirf group ke andar kaam karta hai. Bot ko group me add "
    "karein, video chat start karein, aur wahan command bhejein."
)

# Last player card per chat — the old card is removed when a new one is
# posted so the group doesn't fill up with dead buttons.
_last_card: dict = {}


def _brand_thumb(path: str) -> str:
    """Add VPlay branding (gradient + label) to a thumbnail. Returns path."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return path
    try:
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for y in range(int(h * 0.62), h):
            alpha = int(190 * (y - h * 0.62) / max(1, h * 0.38))
            draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(20, w // 16))
        except Exception:
            font = ImageFont.load_default()
        draw.text((w // 28, h - int(h * 0.14)), "VPlay", fill=(255, 255, 255, 255), font=font)
        out = img.convert("RGB")
        out.paste(overlay, (0, 0), overlay)
        out.save(path, "JPEG", quality=88)
    except Exception:
        return path
    return path


_branded_cache = {}


async def _brand_thumb_async(path: str) -> str:
    """NON-BLOCKING branding — Pillow runs in executor, NOT the event loop.

    ROOT FIX for mid-stream audio stutter: synchronous Image.open()/save()
    froze the asyncio loop for ~6s, starving the ffmpeg audio buffer.
    """
    if path in _branded_cache:
        return _branded_cache[path]
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _brand_thumb, path)
        _branded_cache[path] = result
        return result
    except Exception as e:
        logger.warning("brand_thumb_async fail: %s", e)
        return path


async def _refresh_thumb_id(origin) -> str | None:
    """Re-fetch the ORIGINAL message and return a fresh thumbnail file_id.

    A file_id embeds a file_reference that Telegram invalidates (usually
    < 24h, or as soon as the media is re-fetched). Sending / downloading
    with a stale file_id raises FILE_REFERENCE_EXPIRED. The only fix is to
    fetch the message again and read the new thumbnail from it.
    """
    if not origin or len(origin) != 2:
        return None
    _chat_id, _msg_id = origin
    for cli in (bot, assistant):
        try:
            msg = await cli.get_messages(_chat_id, _msg_id)
            if not msg or getattr(msg, "empty", False):
                continue
            for attr in ("video", "document", "audio", "animation"):
                media = getattr(msg, attr, None)
                if media is None:
                    continue
                t = getattr(media, "thumbnail", None) or getattr(media, "thumb", None)
                if t is not None and getattr(t, "file_id", None):
                    return t.file_id
                thumbs = getattr(media, "thumbs", None) or []
                best = None
                for th in thumbs:
                    fid = getattr(th, "file_id", None)
                    if not fid:
                        continue
                    area = (getattr(th, "width", 0) or 0) * (getattr(th, "height", 0) or 0)
                    if best is None or area > best[0]:
                        best = (area, fid)
                if best:
                    return best[1]
        except Exception as e:  # noqa: BLE001
            logger.debug("refresh_thumb via %s: %s", cli.name, e)
    return None


async def _download_fresh_thumb(origin) -> str | None:
    """Fetch the origin message fresh and DOWNLOAD its thumbnail to a local file.

    ROOT FIX: `_refresh_thumb_id` returned a file_id STRING. Telegram's
    thumbnail file_ids are type THUMBNAIL, which send_photo() rejects with
    "Expected PHOTO, got THUMBNAIL". And download_media(file_id_string)
    reconstructs the reference from the stale string -> FILE_REFERENCE_EXPIRED.

    Fetching the message again and passing the *Thumbnail object* to
    download_media() gives Pyrogram a fresh file_reference, so the download
    actually works. Source: Telegram file references expire (~24h) and must
    be refreshed by re-fetching the message.
    """
    if not origin or len(origin) != 2:
        return None
    _chat_id, _msg_id = origin

    for cli in (bot, assistant):
        try:
            msg = await cli.get_messages(_chat_id, _msg_id)
            if not msg or getattr(msg, "empty", False):
                continue

            for attr in ("video", "document", "audio", "animation"):
                media = getattr(msg, attr, None)
                if media is None:
                    continue

                # 1a. Try the single thumbnail object
                thumb = getattr(media, "thumbnail", None) or getattr(media, "thumb", None)
                if thumb is not None:
                    try:
                        path = await bot.download_media(thumb)
                        if path:
                            logger.info("fresh thumb downloaded (single): %s", path)
                            return path
                    except Exception as e:  # noqa: BLE001
                        logger.info("download fresh single thumb fail: %s", e)

                # 1b. Try largest from thumbs list
                thumbs = getattr(media, "thumbs", None) or []
                if thumbs:
                    try:
                        ordered = sorted(
                            thumbs,
                            key=lambda x: (getattr(x, "width", 0) or 0) * (getattr(x, "height", 0) or 0),
                            reverse=True,
                        )
                    except Exception:  # noqa: BLE001
                        ordered = list(thumbs)
                    for th in ordered:
                        try:
                            path = await bot.download_media(th)
                            if path:
                                logger.info("fresh thumb downloaded (list): %s", path)
                                return path
                        except Exception as e:  # noqa: BLE001
                            logger.debug("thumb variant fail: %s", e)
                            continue
        except Exception as e:  # noqa: BLE001
            logger.info("download_fresh_thumb via %s: %s", cli.name, e)

    return None


def _is_expired_ref(e: BaseException) -> bool:
    low = f"{type(e).__name__} {e}".lower()
    return (
        "filereferenceexpired" in low
        or "file_reference_expired" in low
        or "fileref_upgrade_needed" in low
    )


async def _send_photo_card(chat_id: int, thumb_id: str, text: str, markup,
                           origin=None) -> Message:
    """Send photo card. Fresh thumbnail download FIRST, then stored id fallback.

    Root fix for FILE_REFERENCE_EXPIRED: always re-fetch the origin message
    and download the thumbnail object (fresh file_reference) before touching
    the stored thumb_id. The stored file_id string becomes useless once its
    file_reference expires (~24h).
    """
    async def _send_path(path):
        return await bot.send_photo(
            chat_id, path, caption=text, reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )

    # ---------- Path 1: fresh download from origin ----------
    if origin:
        try:
            fresh_path = await _download_fresh_thumb(origin)
            if fresh_path:
                branded = await _brand_thumb_async(fresh_path)
                try:
                    return await _send_path(branded)
                finally:
                    try:
                        os.remove(fresh_path)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            logger.info("fresh thumbnail path failed: %s", e)

    # ---------- Path 2: try stored thumb_id directly (fast if fresh) ----------
    if thumb_id:
        try:
            return await bot.send_photo(
                chat_id, thumb_id, caption=text, reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # noqa: BLE001
            logger.info("direct send_photo(%s…): %s", thumb_id[:20], type(e).__name__)

    # ---------- Path 3: download stored id (may still work) ----------
    if thumb_id:
        path = None
        try:
            path = await bot.download_media(thumb_id)
            if path:
                path = await _brand_thumb_async(path)
                return await _send_path(path)
        except Exception as e:  # noqa: BLE001
            logger.info("download_media(%s…): %s", thumb_id[:20], type(e).__name__)
        finally:
            if path:
                try:
                    os.remove(path)
                except Exception:  # noqa: BLE001
                    pass

    raise RuntimeError("all thumbnail paths failed")


async def _post_card(chat_id: int, text: str, edit_message: Message = None,
                     markup=None, use_thumb: bool = True) -> None:
    state = get_state(chat_id)
    if markup is None:
        markup = ui.player_markup(chat_id, state)
    thumb = None
    _origin = None
    if use_thumb and state.current is not None:
        thumb = getattr(state.current, "thumb_id", None)
        # chat_id/message_id of the ORIGINAL media message, used to refresh an
        # expired file reference when sending the thumbnail.
        try:
            _parts = str(getattr(state.current, "source", "")).rstrip("/").split("/")
            if len(_parts) >= 2:
                _origin = (int(_parts[-2]), int(_parts[-1]))
        except (TypeError, ValueError):
            _origin = None
        if not thumb:
            try:
                from bot.database.owner_data import get_default_thumb
                thumb = get_default_thumb()
            except Exception:
                thumb = None
    old = _last_card.pop(chat_id, None)
    msg = None
    if edit_message is not None:
        try:
            _is_photo = bool(getattr(edit_message, "photo", None))
            if thumb:
                if _is_photo:
                    msg = await edit_message.edit_caption(caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
                else:
                    try:
                        await edit_message.delete()
                    except Exception:  # noqa: BLE001
                        pass
                    msg = await _send_photo_card(chat_id, thumb, text, markup, origin=_origin)
            else:
                if _is_photo:
                    try:
                        await edit_message.delete()
                    except Exception:  # noqa: BLE001
                        pass
                    msg = await bot.send_message(chat_id, text, reply_markup=markup, **ui.HTML, **ui.NO_PREVIEW)
                else:
                    msg = await edit_message.edit_text(text, reply_markup=markup, **ui.HTML)
        except MessageNotModified:
            msg = edit_message
        except Exception as e:  # noqa: BLE001
            logger.info("Card edit fail (%s) — fresh send", e)
            edit_message = None
    if msg is None:
        if thumb:
            try:
                msg = await _send_photo_card(chat_id, thumb, text, markup, origin=_origin)
            except Exception as e:  # noqa: BLE001
                logger.warning("send_photo fail (%s) — fallback to text", e)
                msg = await bot.send_message(chat_id, text, reply_markup=markup, **ui.HTML, **ui.NO_PREVIEW)
        else:
            msg = await bot.send_message(chat_id, text, reply_markup=markup, **ui.HTML, **ui.NO_PREVIEW)
    if old and msg is not None and old != msg.id:
        try:
            await bot.delete_messages(chat_id, old)
        except Exception:  # noqa: BLE001
            pass
    if msg is not None:
        _last_card[chat_id] = msg.id


async def _drop_card(chat_id: int) -> None:
    old = _last_card.pop(chat_id, None)
    if old:
        try:
            await bot.delete_messages(chat_id, old)
        except Exception:  # noqa: BLE001
            pass


async def _safe_edit(message: Message, text: str, markup=None) -> None:
    """Edit text OR caption based on message type. Never raises."""
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
        else:
            await message.edit_text(text, reply_markup=markup, **ui.HTML)
    except MessageNotModified:
        pass
    except Exception as e:  # noqa: BLE001
        logger.info("_safe_edit fail: %s", e)


async def _on_track_change(chat_id: int, track, reason: str = "ended") -> None:
    if track is None:
        await _drop_card(chat_id)
        if reason == "closed":
            text = ("⏹ <b>Video chat band ho gayi</b> (ya assistant hata diya gaya) — "
                    "queue clear.\nDobara chalane ke liye <code>/vplay</code> bhejein.")
        else:
            text = ("✅ <b>Queue khatam</b> — video chat se nikal gaya.\n"
                    "Naya track chalane ke liye <code>/vplay</code> bhejein.")
        await bot.send_message(chat_id, text)
        return
    await _post_card(chat_id, ui.now_playing_card(get_state(chat_id)))


player_core.on_track_change = _on_track_change


def _register_private_hint(commands):
    @bot.on_message(cmd(commands) & _PRIVATE)
    @error_guard
    async def _hint(client, message: Message):
        await message.reply_text(_PRIVATE_HINT)

    return _hint


# Pending "kaunsa play karein?" lists: token -> {chat, entries, ts, force, user, query, suggested}
_pending_picks: dict = {}
_PICK_TTL = 15 * 60
_search_locks: dict = {}


def _new_pick(chat_id: int, entries: list, force: bool, user_id: int, query: str,
              suggested: bool) -> str:
    import secrets
    import time as _t
    now = _t.time()
    for k in [k for k, v in _pending_picks.items() if now - v["ts"] > _PICK_TTL]:
        _pending_picks.pop(k, None)
    token = secrets.token_hex(4)
    _pending_picks[token] = {
        "chat": chat_id, "entries": entries, "ts": now, "force": force,
        "user": user_id, "query": query, "suggested": suggested,
    }
    return token


async def _maybe_show_choices(message: Message, force: bool) -> bool:
    """/vplay <name>: search the movie channel(s).

    * exactly one match  -> play it straight away
    * several matches    -> colored button list, LATEST upload on top (green)
    * no exact match     -> "kya aap ye dhoondh rahe the?" suggestions
    Returns True when the request was handled here.
    """
    from bot.core import movie_search

    if not movie_search.looks_like_name_query(message):
        return False
    query = movie_search.query_from_text(message)
    if not query:
        return False

    user_id = message.from_user.id if message.from_user else 0
    status = await message.reply_text(
        f"🔍 <b>Search ho raha hai…</b>  <code>{html.escape(query[:60])}</code>", **ui.HTML
    )
    # One search per chat at a time — a spammed /vplay can't flood Telegram.
    lock = _search_locks.setdefault(message.chat.id, asyncio.Lock())
    try:
        async with lock:
            entries, suggested = await movie_search.find(query, limit=30)
    except Exception as e:  # noqa: BLE001
        logger.warning("movie find fail: %s", e)
        entries, suggested = [], False

    if not entries:
        await _safe_edit(status, movie_search.not_found_text(query))
        return True

    if len(entries) == 1 and not suggested:
        await _play_entry(message.chat.id, entries[0], message.from_user, force, status=status)
        return True

    token = _new_pick(message.chat.id, entries, force, user_id, query, suggested)
    await _safe_edit(
        status,
        ui.search_results_text(query, entries, 0, suggested),
        ui.search_results_markup(token, entries, 0, suggested),
    )
    return True


async def _fetch_entry_message(entry):
    """Fresh message for an index entry -> (client, message) or (None, None)."""
    for cli in (assistant, bot):
        try:
            m = await cli.get_messages(entry.chat_id, entry.msg_id)
            if m and not getattr(m, "empty", False):
                return cli, m
        except Exception as e:  # noqa: BLE001
            logger.info("pick get_messages via %s: %s", cli.name, e)
    return None, None


async def _play_entry(chat_id: int, entry, user, force: bool, reply_to: Message = None,
                      status: Message = None) -> None:
    from bot.core import movie_index, movie_search

    cli, msg = await _fetch_entry_message(entry)
    if msg is None or movie_search._media_of(msg)[0] is None:  # noqa: SLF001
        movie_index.forget(entry.chat_id, entry.msg_id)
        text = "⚠️ Ye file ab channel me nahi hai (delete ho gayi). Dobara search karein."
        if status is not None:
            await _safe_edit(status, text)
        elif reply_to is not None:
            await reply_to.reply_text(text)
        return
    user_name = getattr(user, "first_name", None) or "User"
    user_id = getattr(user, "id", 0) or 0
    track = movie_search.build_track(msg, user_name, user_id, "channel", client=cli)
    await _start_track(chat_id, track, user_name, user_id, force, reply_to=reply_to, status=status)

async def _start_track(chat_id: int, track, user_name: str, user_id: int, force: bool,
                       reply_to: Message = None, status: Message = None) -> None:
    _state = get_state(chat_id)
    if not force:
        dup = None
        if _state.current and _state.current.title == track.title:
            dup = ("⚠️ <b>Ye track pehle se chal raha hai.</b>\n"
                   "Seek: <code>/vseek 90</code>  ·  Skip: <code>/vskip</code>")
        else:
            for _i, _q in enumerate(_state.queue, 1):
                if _q.title == track.title:
                    dup = (f"⚠️ <b>Ye track pehle se queue me hai</b> (position #{_i}).\n"
                           "Dekhne ke liye: <code>/vqueue</code>")
                    break
        if dup:
            if status is not None:
                await status.edit_text(dup, **ui.HTML)
            else:
                await reply_to.reply_text(dup, **ui.HTML)
            return

    text = ("⚡ <b>Force-play</b> — stream replace ho raha hai…" if force
            else "⚡ <b>Stream start ho raha hai…</b>")
    if status is not None:
        await status.edit_text(text, **ui.HTML)
    else:
        status = await reply_to.reply_text(text, **ui.HTML)
    try:
        await player_core.ensure_assistant_in_chat(chat_id)
        if force:
            _state.queue.clear()
            if _state.current is not None:
                await player_core.stop(chat_id)
        started = await player_core.play_or_queue(chat_id, track)
    except Exception as e:  # noqa: BLE001
        logger.warning("start_track[%s] fail: %s", chat_id, e)
        try:
            await status.edit_text(f"⚠️ {html.escape(str(e))[:800]}", **ui.HTML)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        chat = await bot.get_chat(chat_id)
        title = getattr(chat, "title", "") or "Group"
        await add_served_chat(chat_id, title)
        if user_id:
            await add_served_user(user_id, user_name)
        await increment_play_count()
        await log_play(title, chat_id, user_name, user_id, track.title,
                       "force" if force else track.mode)
    except Exception as e:  # noqa: BLE001 - stats must never break playback
        logger.warning("Stats/log update fail: %s", e)

    logger.info("vplay[%s]: %s", chat_id, "playing" if started else "queued")
    if started:
        await _post_card(chat_id, ui.now_playing_card(get_state(chat_id)), edit_message=status)
    else:
        await status.edit_text(ui.queued_card(track, len(get_state(chat_id).queue)))


async def _can_use_pick(client, query: CallbackQuery, pick: dict) -> bool:
    uid = query.from_user.id if query.from_user else 0
    if not pick.get("user") or uid == pick.get("user") or uid == config.OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(pick["chat"], uid)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:  # noqa: BLE001
        return False


@bot.on_callback_query(filters.regex(r"^vnoop$"))
async def vnoop_cb(client, query: CallbackQuery):
    await query.answer()


@bot.on_callback_query(filters.regex(r"^vpage\|([0-9a-f]+)\|(\d+)$"))
async def vpage_cb(client, query: CallbackQuery):
    _, token, page = query.data.split("|")
    pick = _pending_picks.get(token)
    if pick is None or query.message is None or query.message.chat.id != pick["chat"]:
        await query.answer("⌛ Ye list expire ho gayi. /vplay dobara karein.", show_alert=True)
        return
    page = int(page)
    await query.answer(f"📄 Page {page + 1}")
    await _safe_edit(
        query.message,
        ui.search_results_text(pick["query"], pick["entries"], page, pick["suggested"]),
        ui.search_results_markup(token, pick["entries"], page, pick["suggested"]),
    )


@bot.on_callback_query(filters.regex(r"^vpick\|([0-9a-f]+)\|(\d+|x)$"))
async def vpick_cb(client, query: CallbackQuery):
    _, token, idx = query.data.split("|")
    pick = _pending_picks.get(token)
    if pick is None or query.message is None or query.message.chat.id != pick["chat"]:
        await query.answer("⌛ Ye list expire ho gayi. /vplay dobara karein.", show_alert=True)
        return
    if not await _can_use_pick(client, query, pick):
        await query.answer("🔒 Ye list kisi aur ne request ki hai. Apna /vplay bhejein.", show_alert=True)
        return
    if idx == "x":
        _pending_picks.pop(token, None)
        await query.answer("❌ Cancelled")
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        return
    i = int(idx)
    if i >= len(pick["entries"]):
        await query.answer("Invalid choice", show_alert=True)
        return
    _pending_picks.pop(token, None)  # one tap only — no double play
    entry = pick["entries"][i]
    await query.answer(f"▶️ {entry.title[:40]}")
    await _safe_edit(
        query.message,
        f"⚡ <b>Stream start ho raha hai…</b>\n🎬 {html.escape(entry.title[:60])}"
        + (f" ({entry.year})" if entry.year else "")
        + (f"  ·  {entry.quality}" if entry.quality else ""),
    )
    await _play_entry(pick["chat"], entry, query.from_user, pick["force"], status=query.message)


@bot.on_message(cmd(["vindex", "vreindex"]))
@error_guard
async def vindex_cmd(client, message: Message):
    """Movie channel index status. Owner: `/vindex rebuild` to re-scan now."""
    import time as _t
    from bot.core import movie_index, streamer

    parts = (message.text or "").split()
    is_owner = bool(message.from_user and message.from_user.id == config.OWNER_ID)
    if len(parts) > 1 and parts[1].lower() in ("rebuild", "refresh", "scan"):
        if not is_owner:
            await message.reply_text("🔒 Sirf owner index rebuild kar sakta hai.")
            return
        m = await message.reply_text("🔄 <b>Movie channels scan ho rahe hain…</b>", **ui.HTML)
        res = await movie_index.rebuild(full=True)
        await _safe_edit(m, f"✅ <b>Index ready</b> — {res['files']:,} files · {res['channels']} channel(s)")
        return
    st = movie_index.status()
    ss = streamer.stats()
    ago = int(_t.time() - st["last_refresh"]) if st["last_refresh"] else None
    lines = [
        "📚 <b>Movie Index</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Status: {'🟢 Ready' if st['ready'] else ('⏳ Ban raha hai' if st['building'] else '🔴 Not ready')}",
        f"Files: <b>{st['files']:,}</b>",
        f"Last scan: {f'{ago // 60} min pehle' if ago is not None else '—'}",
        "",
        f"📡 <b>Channels ({len(st['channels'])})</b>",
    ]
    for cid, title in st["channels"][:10]:
        lines.append(f"   • {html.escape(title or 'Channel')} <code>{cid}</code>")
    if not st["channels"]:
        lines.append("   <i>Koi nahi — assistant ko movie channel me admin banayein</i>")
    if st["error"]:
        lines.append(f"\n⚠️ <code>{html.escape(st['error'][:150])}</code>")
    lines += ["", "⚡ <b>Streamer</b>",
              f"   Files: {ss['files']} · Cache: {ss['cached_mb']} MB · Readers: {ss['readers']}"]
    if is_owner:
        lines.append("\n<i>Rebuild: /vindex rebuild</i>")
    await message.reply_text("\n".join(lines), **ui.HTML)

@bot.on_message(cmd(["vplay", "play"]) & _GROUP)
@error_guard
@admin_only
async def vplay_cmd(client, message: Message):
    chat_id = message.chat.id
    user_name = message.from_user.first_name if message.from_user else (
        getattr(message.sender_chat, "title", None) or "Admin")
    user_id = message.from_user.id if message.from_user else 0

    # /vplay <name> -> show all matches as buttons (user picks which one).
    if await _maybe_show_choices(message, force=False):
        return

    track = await resolve_track(message, user_name, user_id, client)  # reply / link
    await _start_track(chat_id, track, user_name, user_id, False, reply_to=message)


@bot.on_message(cmd(["vplayforce", "playforce"]) & _GROUP)
@error_guard
@admin_only
async def vplayforce_cmd(client, message: Message):
    """Force-play: bypass queue, immediately replace current stream."""
    chat_id = message.chat.id
    user_name = message.from_user.first_name if message.from_user else (
        getattr(message.sender_chat, "title", None) or "Admin")
    user_id = message.from_user.id if message.from_user else 0

    if await _maybe_show_choices(message, force=True):
        return

    track = await resolve_track(message, user_name, user_id, client)

    status = await message.reply_text("⚡ <b>Force-play</b> — stream replace ho raha hai…", **ui.HTML)
    try:
        await player_core.ensure_assistant_in_chat(chat_id)
        # Force: clear queue + stop current, then play new
        state = get_state(chat_id)
        state.queue.clear()
        if state.current is not None:
            await player_core.stop(chat_id)
        started = await player_core.play_or_queue(chat_id, track)
    except Exception:
        try:
            await status.delete()
        except Exception:
            pass
        raise

    try:
        await add_served_chat(chat_id, message.chat.title or "")
        if user_id:
            await add_served_user(user_id, user_name)
        await increment_play_count()
        await log_play(message.chat.title or "Group", chat_id, user_name, user_id, track.title, "force")
    except Exception as e:
        logger.warning("Stats/log update fail: %s", e)

    logger.info("vplayforce[%s]: playing", chat_id)
    await _post_card(chat_id, ui.now_playing_card(get_state(chat_id)), edit_message=status)


@bot.on_message(cmd("vpause") & _GROUP)
@error_guard
@admin_only
async def vpause_cmd(client, message: Message):
    await player_core.pause(message.chat.id)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(get_state(message.chat.id), "⏸ <b>Paused</b>"),
    )


@bot.on_message(cmd("vresume") & _GROUP)
@error_guard
@admin_only
async def vresume_cmd(client, message: Message):
    await player_core.resume(message.chat.id)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(get_state(message.chat.id), "▶️ <b>Resumed</b>"),
    )


@bot.on_message(cmd("vskip") & _GROUP)
@error_guard
@admin_only
async def vskip_cmd(client, message: Message):
    nxt = await player_core.skip(message.chat.id)
    if nxt:
        await _post_card(message.chat.id, ui.now_playing_card(get_state(message.chat.id), "⏭ <b>Skipped — ab chal raha hai</b>"))
    else:
        await _drop_card(message.chat.id)
        await message.reply_text(
        "⏭ <b>Queue khali hai</b> — agla track nahi hai.\n"
        "<code>/vplay</code> se naya track add karein."
    )


@bot.on_message(cmd(["vstop", "vend"]) & _GROUP)
@error_guard
@admin_only
async def vstop_cmd(client, message: Message):
    if get_state(message.chat.id).current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    await player_core.stop(message.chat.id)
    await _drop_card(message.chat.id)
    who = message.from_user.first_name if message.from_user else "admin"
    await log_stop(message.chat.title or "Group", message.chat.id, who)
    await message.reply_text(
        f"⏹ <b>Playback stopped</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Stopped by:</b> {html.escape(who)}\n"
        "📜 Queue clear, video chat chhod diya gaya.\n\n"
        "<i>Naya track chalane ke liye</i> <code>/vplay</code> <i>bhejein.</i>"
    )


@bot.on_message(cmd(["vseek", "seek"]) & _GROUP)
@error_guard
@admin_only
async def vseek_cmd(client, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        _cur = format_duration(get_state(message.chat.id).elapsed())
        await message.reply_text(
            f"Usage: <code>/vseek 90</code> ya <code>/vseek 1:30</code>\n"
            f"Abhi: <b>{_cur}</b> pe chal raha hai."
        )
        return
    seconds = parse_time_to_seconds(parts[1].split()[0])
    used = await player_core.seek(message.chat.id, seconds)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(
            get_state(message.chat.id),
            f"⏩ <b>Seeked to {format_duration(used)}</b>",
        ),
    )




@bot.on_message(cmd(["seekback", "vseekback"]) & _GROUP)
@error_guard
@admin_only
async def seekback_cmd(client, message: Message):
    """Seek backwards by N seconds. Usage: /seekback 30 or /seekback 1m"""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        _cur = format_duration(get_state(message.chat.id).elapsed())
        await message.reply_text(
            f"Usage: `/seekback 30` ya `/seekback 1m`\n"
            f"Abhi: {_cur} pe chal raha hai."
        )
        return
    try:
        delta = parse_time_to_seconds(parts[1].split()[0])
    except ValueError as e:
        raise ValueError(str(e))
    used = await player_core.seek_relative(message.chat.id, -delta)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(
            get_state(message.chat.id),
            f"⏪ Seeked back {format_duration(delta)} → {format_duration(used)}",
        ),
    )


@bot.on_message(cmd("vloop") & _GROUP)
@error_guard
@admin_only
async def vloop_cmd(client, message: Message):
    mode = player_core.cycle_loop_mode(message.chat.id)
    _label = {"off": "OFF", "track": "TRACK", "queue": "QUEUE"}.get(mode, mode.upper())
    await _post_card(
        message.chat.id,
        ui.now_playing_card(
            get_state(message.chat.id),
            f"🔁 <b>Loop: {_label}</b>",
        ),
    )


@bot.on_message(cmd("vshuffle") & _GROUP)
@error_guard
@admin_only
async def vshuffle_cmd(client, message: Message):
    n = player_core.shuffle(message.chat.id)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(get_state(message.chat.id), f"🔀 <b>{n} tracks shuffled</b>"),
    )


@bot.on_message(cmd(["vqueue", "vplaylist"]) & _GROUP)
@error_guard
async def vqueue_cmd(client, message: Message):
    state = get_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    await _post_card(
        message.chat.id,
        ui.queue_text(state),
        markup=ui.queue_markup(message.chat.id),
        use_thumb=False,
    )


@bot.on_message(cmd(["vnowplaying", "vnp", "vcurrent"]) & _GROUP)
@error_guard
async def vnowplaying_cmd(client, message: Message):
    if get_state(message.chat.id).current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    await _post_card(message.chat.id, ui.now_playing_card(get_state(message.chat.id)))


@bot.on_message(cmd("vmute") & _GROUP)
@error_guard
@admin_only
async def vmute_cmd(client, message: Message):
    await player_core.mute(message.chat.id)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(get_state(message.chat.id), "🔇 <b>Muted</b>"),
    )


@bot.on_message(cmd("vunmute") & _GROUP)
@error_guard
@admin_only
async def vunmute_cmd(client, message: Message):
    await player_core.unmute(message.chat.id)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(get_state(message.chat.id), "🔊 <b>Unmuted</b>"),
    )


# --- Player card buttons -------------------------------------------------

_ACTIONS = "back30|fwd30|pause|resume|skip|stop|mute|unmute|queue|close|back|fwd|loop|refresh"


async def _refresh_card(query: CallbackQuery, chat_id: int) -> None:
    state = get_state(chat_id)
    if state.current is None:
        return
    try:
        if getattr(query.message, "photo", None):
            await query.message.edit_caption(
                caption=ui.now_playing_card(state),
                reply_markup=ui.player_markup(chat_id, state),
            )
        else:
            await query.message.edit_text(
                ui.now_playing_card(state),
                reply_markup=ui.player_markup(chat_id, state),
            )
    except MessageNotModified:
        pass
    except Exception as e:  # noqa: BLE001
        logger.info("Refresh card fail: %s", e)


@bot.on_callback_query(filters.regex(rf"^cb_({_ACTIONS})_(-?\d+)$"))
async def player_buttons(client, query: CallbackQuery):
    _, action, chat_str = query.data.split("_", 2)
    chat_id = int(chat_str)

    answered = False

    async def _answer(text: str = None, alert: bool = False) -> None:
        nonlocal answered
        if answered:
            return
        answered = True
        try:
            await query.answer(text, show_alert=alert)
        except Exception:  # noqa: BLE001
            pass

    # Buttons must act on the chat they are shown in (callback data can be
    # crafted by custom clients).
    if query.message is None or query.message.chat is None or query.message.chat.id != chat_id:
        await _answer("Ye button is chat ka nahi hai.", True)
        return

    if action not in ("queue", "refresh") and config.AUTH_ONLY_ADMINS and query.from_user.id != config.OWNER_ID:
        try:
            member = await client.get_chat_member(chat_id, query.from_user.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await _answer("🔒 Sirf admins ye button use kar sakte hain.", True)
                return
        except Exception:  # noqa: BLE001
            await _answer("Permission check nahi ho paya.", True)
            return

    state = get_state(chat_id)
    if action != "close" and state.current is None:
        await _answer("Abhi kuch play nahi ho raha.", True)
        try:
            await query.message.edit_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        if action == "pause":
            await player_core.pause(chat_id)
            await _answer("⏸ Paused")
            await _refresh_card(query, chat_id)
        elif action == "resume":
            await player_core.resume(chat_id)
            await _answer("▶️ Resumed")
            await _refresh_card(query, chat_id)
        elif action in ("back", "fwd", "back30", "fwd30"):
            await _answer("⏳ Seeking…")
            delta = {"back": -10, "fwd": 10, "back30": -30, "fwd30": 30}[action]
            pos = await player_core.seek_relative(chat_id, delta)
            await _refresh_card(query, chat_id)
            logger.info("Button seek %s chat %s -> %ss", action, chat_id, pos)
        elif action == "skip":
            await _answer("⏭ Skipping…")
            nxt = await player_core.skip(chat_id)
            if nxt:
                await _post_card(chat_id, ui.now_playing_card(get_state(chat_id), "⏭ <b>Skipped — ab chal raha hai</b>"),
                                 edit_message=query.message)
            else:
                _last_card.pop(chat_id, None)
                await _safe_edit(
                    query.message,
                    "⏭ <b>Queue khali hai</b> — video chat se nikal gaya.\n\n"
                    "<i>Naya track chalane ke liye</i> <code>/vplay</code> <i>bhejein.</i>",
                )
        elif action == "stop":
            await _answer("⏹ Stopping…")
            await player_core.stop(chat_id)
            _last_card.pop(chat_id, None)
            who = query.from_user.first_name or "admin"
            _stop_msg = (
                f"⏹ <b>Playback stopped</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Stopped by:</b> {html.escape(who)}\n"
                f"📜 Queue clear, video chat chhod diya gaya.\n\n"
                f"<i>Naya track chalane ke liye</i> <code>/vplay</code> <i>bhejein.</i>"
            )
            await _safe_edit(query.message, _stop_msg)
            try:
                await log_stop(query.message.chat.title or "Group", chat_id, who)
            except Exception as e:  # noqa: BLE001
                logger.warning("log_stop fail: %s", e)
        elif action == "loop":
            on = player_core.toggle_loop(chat_id)
            _st = get_state(chat_id)
            _lm = getattr(_st, "loop_mode", "off")
            _label = {"off": "OFF", "track": "TRACK", "queue": "QUEUE"}.get(_lm, _lm.upper())
            await _answer(f"🔁 Loop: {_label}")
            await _refresh_card(query, chat_id)
        elif action == "mute":
            await player_core.mute(chat_id)
            await _answer("🔇 Muted")
            await _refresh_card(query, chat_id)
        elif action == "unmute":
            await player_core.unmute(chat_id)
            await _answer("🔊 Unmuted")
            await _refresh_card(query, chat_id)
        elif action == "refresh":
            await _refresh_card(query, chat_id)
            await _answer("🔄 Updated")
        elif action == "queue":
            names = "\n".join(f"{i}. {t.title[:40]}" for i, t in enumerate(state.queue[:5], 1)) or "(khali)"
            text = f"▶️ {state.current.title[:50]}\n\nNext:\n{names}"
            if len(state.queue) > 5:
                text += f"\n…+{len(state.queue) - 5}"
            await _answer(text[:199], True)
        elif action == "close":
            _last_card.pop(chat_id, None)
            await _answer()
            await query.message.delete()
    except Exception as e:  # noqa: BLE001
        logger.warning("Button %s failed in %s: %s", action, chat_id, player_core.describe_error(e))
        reason = (str(e).split("\n")[0] or type(e).__name__)[:190]
        if not answered:
            await _answer(reason, True)
        else:
            # The toast was already used ("Seeking…" / "Skipping…") — tell
            # the user in the chat instead of failing silently.
            try:
                await bot.send_message(chat_id, f"⚠️ {html.escape(reason)}")
            except Exception:  # noqa: BLE001
                pass
    finally:
        await _answer()  # never leave Telegram's loading spinner hanging


_register_private_hint(
    [
        "vplay", "play", "vpause", "vresume", "vskip", "vstop", "vend",
        "vseek", "vqueue", "vplaylist", "vnowplaying", "vnp", "vcurrent",
        "vmute", "vunmute", "vloop", "vshuffle", "seek", "vcleanup", "vvolume",
        "vplayforce", "playforce", "seekback", "vseekback",
    ]
)

# --- Welcome when bot is added to a group --------------------------------

from pyrogram import filters as _filters
from pyrogram.types import Message as _Message


@bot.on_message(_filters.new_chat_members)
async def on_bot_added(client, message: _Message):
    me = getattr(client, "me", None) or getattr(bot, "me", None)
    if not me:
        return
    if any(getattr(m, "is_self", False) or m.id == me.id for m in message.new_chat_members):
        try:
            await message.reply_text(
                "👋 <b>VPlay add karne ke liye shukriya!</b>\n\n"
                "Video chat start karein, phir <code>/vplay</code> bhejein.\n"
                "<b>Commands:</b> /vplay, /vseek, /vpause, /vresume, /vskip, /vstop, /vqueue, /vloop"
            )
        except Exception:
            pass

@bot.on_message(cmd("vcleanup") & _GROUP)
@error_guard
@admin_only
async def vcleanup_cmd(client, message: Message):
    """Purani temp files delete karo (admin only)."""
    from bot.core.storage import cleanup_temp_files
    removed = cleanup_temp_files(max_age_hours=0)
    if removed:
        await message.reply_text(f"🧹 <b>Cleanup done</b> — {removed} temp files delete hui.")
    else:
        await message.reply_text("🧹 Cleanup ke liye koi temp file nahi mili.")

@bot.on_message(cmd("vvolume") & _GROUP)
@error_guard
@admin_only
async def vvolume_cmd(client, message: Message):
    """Volume set karo (1-200). Usage: /vvolume 150"""
    parts = (message.text or "").split()
    if len(parts) < 2:
        _st = get_state(message.chat.id)
        await message.reply_text(
            f"🔊 <b>Volume:</b> {getattr(_st, 'volume', 100)}/200\n"
            "Usage: <code>/vvolume 150</code>"
        )
        return
    try:
        vol = int(parts[1])
    except ValueError:
        raise ValueError("Volume 1-200 ke beech ka number hona chahiye.")
    applied = await player_core.set_volume(message.chat.id, vol)
    await _post_card(
        message.chat.id,
        ui.now_playing_card(
            get_state(message.chat.id),
            f"🔊 <b>Volume: {applied}/200</b>",
        ),
    )

