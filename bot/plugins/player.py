import html

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Message

from bot import config
from bot.core import player as player_core
from bot.core.clients import bot
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


async def _post_card(chat_id: int, text: str, edit_message: Message = None) -> None:
    state = get_state(chat_id)
    markup = ui.player_markup(chat_id, state)
    old = _last_card.pop(chat_id, None)
    msg = None
    if edit_message is not None:
        try:
            msg = await edit_message.edit_text(text, reply_markup=markup)
        except MessageNotModified:
            msg = edit_message
        except Exception as e:  # noqa: BLE001 - deleted / too old -> post fresh
            logger.info("Card edit fail (%s) — naya card bhej raha hoon", e)
            edit_message = None
    if msg is None:
        msg = await bot.send_message(chat_id, text, reply_markup=markup)
    if old and (edit_message is None or old != edit_message.id):
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


@bot.on_message(cmd(["vplay", "play"]) & _GROUP)
@error_guard
@admin_only
async def vplay_cmd(client, message: Message):
    chat_id = message.chat.id
    user_name = message.from_user.first_name if message.from_user else (
        getattr(message.sender_chat, "title", None) or "Admin")
    user_id = message.from_user.id if message.from_user else 0

    track = await resolve_track(message, user_name, user_id, client)  # validate before any UI
    # One status message only — every extra edit is a Telegram round-trip
    # that delayed the actual start.
    status = await message.reply_text("⚡ <b>Stream start ho raha hai…</b>")
    try:
        await player_core.ensure_assistant_in_chat(chat_id)
        started = await player_core.play_or_queue(chat_id, track)
    except Exception:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        raise

    try:
        await add_served_chat(chat_id, message.chat.title or "")
        if user_id:
            await add_served_user(user_id, user_name)
        await increment_play_count()
        await log_play(message.chat.title or "Group", chat_id, user_name, user_id, track.title, track.mode)
    except Exception as e:  # noqa: BLE001 - stats must never break playback
        logger.warning("Stats/log update fail: %s", e)

    logger.info("vplay[%s]: %s", chat_id, "playing" if started else "queued")
    if started:
        await _post_card(chat_id, ui.now_playing_card(get_state(chat_id)), edit_message=status)
    else:
        await status.edit_text(ui.queued_card(track, len(get_state(chat_id).queue)))


@bot.on_message(cmd("vpause") & _GROUP)
@error_guard
@admin_only
async def vpause_cmd(client, message: Message):
    await player_core.pause(message.chat.id)
    await message.reply_text("⏸ <b>Paused.</b> /vresume se dobara chalayein.")


@bot.on_message(cmd("vresume") & _GROUP)
@error_guard
@admin_only
async def vresume_cmd(client, message: Message):
    await player_core.resume(message.chat.id)
    await message.reply_text("▶️ <b>Resumed.</b>")


@bot.on_message(cmd("vskip") & _GROUP)
@error_guard
@admin_only
async def vskip_cmd(client, message: Message):
    nxt = await player_core.skip(message.chat.id)
    if nxt:
        await _post_card(message.chat.id, ui.now_playing_card(get_state(message.chat.id), "⏭ <b>Skipped — ab chal raha hai</b>"))
    else:
        await _drop_card(message.chat.id)
        await message.reply_text("⏭ Queue khali hai — video chat se nikal gaya.")


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
    await message.reply_text(f"⏹ <b>Stopped</b> by {html.escape(who)} — video chat chhod di.")


@bot.on_message(cmd("vseek") & _GROUP)
@error_guard
@admin_only
async def vseek_cmd(client, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: <code>/vseek 90</code> ya <code>/vseek 1:30</code>")
        return
    seconds = parse_time_to_seconds(parts[1].split()[0])
    used = await player_core.seek(message.chat.id, seconds)
    await message.reply_text(f"⏩ Ab <b>{format_duration(used)}</b> se chal raha hai.")


@bot.on_message(cmd("vloop") & _GROUP)
@error_guard
@admin_only
async def vloop_cmd(client, message: Message):
    on = player_core.toggle_loop(message.chat.id)
    await message.reply_text("🔁 <b>Loop ON</b> — ye track repeat hoga." if on else "➡️ <b>Loop OFF</b>")


@bot.on_message(cmd("vshuffle") & _GROUP)
@error_guard
@admin_only
async def vshuffle_cmd(client, message: Message):
    n = player_core.shuffle(message.chat.id)
    await message.reply_text(f"🔀 <b>{n} tracks shuffle ho gaye.</b>")


@bot.on_message(cmd(["vqueue", "vplaylist"]) & _GROUP)
@error_guard
async def vqueue_cmd(client, message: Message):
    state = get_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    await message.reply_text(ui.queue_text(state))


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
    await message.reply_text("🔇 <b>Muted.</b>")


@bot.on_message(cmd("vunmute") & _GROUP)
@error_guard
@admin_only
async def vunmute_cmd(client, message: Message):
    await player_core.unmute(message.chat.id)
    await message.reply_text("🔊 <b>Unmuted.</b>")


# --- Player card buttons -------------------------------------------------

_ACTIONS = "pause|resume|skip|stop|mute|unmute|queue|close|back|fwd|loop|refresh"


async def _refresh_card(query: CallbackQuery, chat_id: int) -> None:
    state = get_state(chat_id)
    if state.current is None:
        return
    try:
        await query.message.edit_text(ui.now_playing_card(state), reply_markup=ui.player_markup(chat_id, state))
    except MessageNotModified:
        pass


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
        elif action in ("back", "fwd"):
            await _answer("⏳ Seeking…")
            pos = await player_core.seek_relative(chat_id, -10 if action == "back" else 10)
            await _refresh_card(query, chat_id)
            logger.info("Button seek chat %s -> %ss", chat_id, pos)
        elif action == "skip":
            await _answer("⏭ Skipping…")
            nxt = await player_core.skip(chat_id)
            if nxt:
                await _post_card(chat_id, ui.now_playing_card(get_state(chat_id), "⏭ <b>Skipped — ab chal raha hai</b>"),
                                 edit_message=query.message)
            else:
                _last_card.pop(chat_id, None)
                await query.message.edit_text("⏭ Queue khali hai — video chat se nikal gaya.")
        elif action == "stop":
            await _answer("⏹ Stopping…")
            await player_core.stop(chat_id)
            _last_card.pop(chat_id, None)
            who = query.from_user.first_name or "admin"
            await query.message.edit_text(f"⏹ <b>Stopped</b> by {html.escape(who)} — video chat chhod di.")
            await log_stop(query.message.chat.title or "Group", chat_id, who)
        elif action == "loop":
            on = player_core.toggle_loop(chat_id)
            await _answer("🔁 Loop ON" if on else "➡️ Loop OFF")
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
        "vmute", "vunmute", "vloop", "vshuffle",
    ]
)
