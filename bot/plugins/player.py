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
    if edit_message is not None:
        msg = await edit_message.edit_text(text, reply_markup=markup)
    else:
        msg = await bot.send_message(chat_id, text, reply_markup=markup)
    if old and (edit_message is None or old != edit_message.id):
        try:
            await bot.delete_messages(chat_id, old)
        except Exception:  # noqa: BLE001
            pass
    if msg is not None:
        _last_card[chat_id] = msg.id


async def _on_track_change(chat_id: int, track) -> None:
    if track is None:
        _last_card.pop(chat_id, None)
        await bot.send_message(chat_id, "✅ <b>Queue khatam</b> — video chat se nikal gaya.\n"
                                        "Naya track chalane ke liye <code>/vplay</code> bhejein.")
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

    track = await resolve_track(message, user_name, user_id)  # validate before any UI
    status = await message.reply_text("⚡ <b>Stream ready ho raha hai…</b>")
    try:
        await status.edit_text("🔗 <b>Assistant ko check kar raha hoon…</b>")
        await player_core.ensure_assistant_in_chat(chat_id)
        if get_state(chat_id).current is None:
            await status.edit_text("📡 <b>Video chat se connect ho raha hoon…</b>")
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
        _last_card.pop(message.chat.id, None)
        await message.reply_text("⏭ Queue khali hai — video chat se nikal gaya.")


@bot.on_message(cmd(["vstop", "vend"]) & _GROUP)
@error_guard
@admin_only
async def vstop_cmd(client, message: Message):
    if get_state(message.chat.id).current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    await player_core.stop(message.chat.id)
    _last_card.pop(message.chat.id, None)
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

    if action not in ("queue", "refresh") and config.AUTH_ONLY_ADMINS and query.from_user.id != config.OWNER_ID:
        try:
            member = await client.get_chat_member(chat_id, query.from_user.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await query.answer("🔒 Sirf admins ye button use kar sakte hain.", show_alert=True)
                return
        except Exception:  # noqa: BLE001
            await query.answer("Permission check nahi ho paya.", show_alert=True)
            return

    state = get_state(chat_id)
    if action not in ("close",) and state.current is None:
        await query.answer("Abhi kuch play nahi ho raha.", show_alert=True)
        try:
            await query.message.edit_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        if action == "pause":
            await player_core.pause(chat_id)
            await query.answer("⏸ Paused")
            await _refresh_card(query, chat_id)
        elif action == "resume":
            await player_core.resume(chat_id)
            await query.answer("▶️ Resumed")
            await _refresh_card(query, chat_id)
        elif action in ("back", "fwd"):
            await query.answer("⏳ Seeking…")
            pos = await player_core.seek_relative(chat_id, -10 if action == "back" else 10)
            await _refresh_card(query, chat_id)
            logger.info("Button seek chat %s -> %ss", chat_id, pos)
        elif action == "skip":
            await query.answer("⏭ Skipping…")
            nxt = await player_core.skip(chat_id)
            if nxt:
                await _post_card(chat_id, ui.now_playing_card(get_state(chat_id), "⏭ <b>Skipped — ab chal raha hai</b>"),
                                 edit_message=query.message)
            else:
                _last_card.pop(chat_id, None)
                await query.message.edit_text("⏭ Queue khali hai — video chat se nikal gaya.")
        elif action == "stop":
            await player_core.stop(chat_id)
            _last_card.pop(chat_id, None)
            await query.answer("⏹ Stopped")
            who = query.from_user.first_name or "admin"
            await log_stop(query.message.chat.title or "Group", chat_id, who)
            await query.message.edit_text(f"⏹ <b>Stopped</b> by {html.escape(who)} — video chat chhod di.")
        elif action == "loop":
            on = player_core.toggle_loop(chat_id)
            await query.answer("🔁 Loop ON" if on else "➡️ Loop OFF")
            await _refresh_card(query, chat_id)
        elif action == "mute":
            await player_core.mute(chat_id)
            await query.answer("🔇 Muted")
            await _refresh_card(query, chat_id)
        elif action == "unmute":
            await player_core.unmute(chat_id)
            await query.answer("🔊 Unmuted")
            await _refresh_card(query, chat_id)
        elif action == "refresh":
            await _refresh_card(query, chat_id)
            await query.answer("🔄 Updated")
        elif action == "queue":
            names = "\n".join(f"{i}. {t.title[:40]}" for i, t in enumerate(state.queue[:5], 1)) or "(khali)"
            text = f"▶️ {state.current.title[:50]}\n\nNext:\n{names}"
            if len(state.queue) > 5:
                text += f"\n…+{len(state.queue) - 5}"
            await query.answer(text[:199], show_alert=True)
        elif action == "close":
            _last_card.pop(chat_id, None)
            await query.message.delete()
    except Exception as e:  # noqa: BLE001
        logger.warning("Button %s failed in %s: %s", action, chat_id, player_core.describe_error(e))
        try:
            await query.answer((str(e).split("\n")[0] or "Error")[:190], show_alert=True)
        except Exception:  # noqa: BLE001
            pass


_register_private_hint(
    [
        "vplay", "play", "vpause", "vresume", "vskip", "vstop", "vend",
        "vseek", "vqueue", "vplaylist", "vnowplaying", "vnp", "vcurrent",
        "vmute", "vunmute", "vloop", "vshuffle",
    ]
)
