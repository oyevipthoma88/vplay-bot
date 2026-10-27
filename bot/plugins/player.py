import html

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import config
from bot.core import player as player_core
from bot.core.clients import bot
from bot.core.logger_gc import log_play, log_stop
from bot.core.queue import Track, get_state
from bot.database import add_served_chat, add_served_user, increment_play_count
from bot.logger import logger
from bot.utils.commands import cmd
from bot.utils.decorators import admin_only, error_guard
from bot.utils.extract import resolve_source
from bot.utils.formatting import format_duration, parse_time_to_seconds

_GROUP = filters.group
_PRIVATE = filters.private

_PRIVATE_HINT = (
    "Ye command sirf group ke andar kaam karta hai. Bot ko group me add "
    "karein, video chat start karein, aur wahan command bhejein."
)


def player_markup(chat_id: int, is_paused: bool = False) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton("▶️ Resume", callback_data=f"cb_resume_{chat_id}")
        if is_paused
        else InlineKeyboardButton("⏸ Pause", callback_data=f"cb_pause_{chat_id}")
    )
    return InlineKeyboardMarkup(
        [
            [
                toggle,
                InlineKeyboardButton("⏭ Skip", callback_data=f"cb_skip_{chat_id}"),
                InlineKeyboardButton("⏹ Stop", callback_data=f"cb_stop_{chat_id}"),
            ],
            [
                InlineKeyboardButton("🔇 Mute", callback_data=f"cb_mute_{chat_id}"),
                InlineKeyboardButton("🔊 Unmute", callback_data=f"cb_unmute_{chat_id}"),
                InlineKeyboardButton("📜 Queue", callback_data=f"cb_queue_{chat_id}"),
            ],
            [InlineKeyboardButton("❌ Close", callback_data=f"cb_close_{chat_id}")],
        ]
    )


def _now_card(title: str, by: str) -> str:
    return (
        "🎬 <b>Ab chal raha hai</b>\n\n"
        f"📌 <b>Title:</b> <i>{html.escape(title)}</i>\n"
        f"👤 <b>Requested by:</b> {html.escape(by)}\n"
        "⚡ <b>Mode:</b> 0s instant stream"
    )


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
    status = await message.reply_text("⚡ Stream ready kar raha hoon...")
    try:
        await player_core.ensure_assistant_in_chat(chat_id)
        source, title, is_url = await resolve_source(message)
    except Exception:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        raise

    user_name = message.from_user.first_name if message.from_user else "someone"
    user_id = message.from_user.id if message.from_user else 0
    track = Track(source=source, title=title, requested_by=user_name, is_url=is_url)
    try:
        started = await player_core.play_or_queue(chat_id, track)
    except Exception:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        raise

    await add_served_chat(chat_id, message.chat.title or "")
    if user_id:
        await add_served_user(user_id, user_name)
    await increment_play_count()
    await log_play(message.chat.title or "Group", chat_id, user_name, user_id, title, is_url)

    logger.info("vplay[%s]: %s", chat_id, "playing" if started else "queued")
    if started:
        await status.edit_text(_now_card(title, user_name), reply_markup=player_markup(chat_id))
    else:
        pos = len(get_state(chat_id).queue)
        await status.edit_text(f"➕ <b>Queue me add (#{pos}):</b> <i>{html.escape(title)}</i>")


@bot.on_message(cmd("vpause") & _GROUP)
@error_guard
@admin_only
async def vpause_cmd(client, message: Message):
    await player_core.pause(message.chat.id)
    await message.reply_text("⏸ Paused.", reply_markup=player_markup(message.chat.id, True))


@bot.on_message(cmd("vresume") & _GROUP)
@error_guard
@admin_only
async def vresume_cmd(client, message: Message):
    await player_core.resume(message.chat.id)
    await message.reply_text("▶️ Resumed.", reply_markup=player_markup(message.chat.id))


@bot.on_message(cmd("vskip") & _GROUP)
@error_guard
@admin_only
async def vskip_cmd(client, message: Message):
    if get_state(message.chat.id).current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    nxt = await player_core.skip(message.chat.id)
    if nxt:
        await message.reply_text(_now_card(nxt.title, nxt.requested_by), reply_markup=player_markup(message.chat.id))
    else:
        await message.reply_text("⏭ Queue khali hai — video chat se nikal gaya.")


@bot.on_message(cmd(["vstop", "vend"]) & _GROUP)
@error_guard
@admin_only
async def vstop_cmd(client, message: Message):
    await player_core.stop(message.chat.id)
    who = message.from_user.first_name if message.from_user else "admin"
    await log_stop(message.chat.title or "Group", message.chat.id, who)
    await message.reply_text("⏹ Ruk gaya aur video chat chhod di.")


@bot.on_message(cmd("vseek") & _GROUP)
@error_guard
@admin_only
async def vseek_cmd(client, message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: /vseek <seconds ya mm:ss>")
        return
    try:
        seconds = parse_time_to_seconds(parts[1].split()[0])
    except (ValueError, IndexError):
        await message.reply_text("Time samajh nahi aaya. Example: /vseek 90 ya /vseek 1:30")
        return
    await player_core.seek(message.chat.id, seconds)
    await message.reply_text(f"⏩ Ab {format_duration(seconds)} se chal raha hai.")


@bot.on_message(cmd(["vqueue", "vplaylist"]) & _GROUP)
@error_guard
async def vqueue_cmd(client, message: Message):
    state = get_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    lines = [f"▶️ <b>Now:</b> <i>{html.escape(state.current.title)}</i>", ""]
    for i, t in enumerate(state.queue[:25], start=1):
        lines.append(f"{i}. {html.escape(t.title)} — {html.escape(t.requested_by)}")
    if len(state.queue) > 25:
        lines.append(f"...aur {len(state.queue) - 25}.")
    if not state.queue:
        lines.append("(queue khali hai)")
    await message.reply_text("\n".join(lines))


@bot.on_message(cmd(["vnowplaying", "vnp", "vcurrent"]) & _GROUP)
@error_guard
async def vnowplaying_cmd(client, message: Message):
    state = get_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    status = "⏸ Paused" if state.is_paused else "▶️ Playing"
    await message.reply_text(
        f"<b>{status}:</b> <i>{html.escape(state.current.title)}</i>\n"
        f"⏱ <b>Elapsed:</b> {format_duration(state.elapsed())}\n"
        f"👤 <b>Requested by:</b> {html.escape(state.current.requested_by)}",
        reply_markup=player_markup(message.chat.id, state.is_paused),
    )


@bot.on_message(cmd("vmute") & _GROUP)
@error_guard
@admin_only
async def vmute_cmd(client, message: Message):
    await player_core.mute(message.chat.id)
    await message.reply_text("🔇 Muted.")


@bot.on_message(cmd("vunmute") & _GROUP)
@error_guard
@admin_only
async def vunmute_cmd(client, message: Message):
    await player_core.unmute(message.chat.id)
    await message.reply_text("🔊 Unmuted.")


@bot.on_callback_query(filters.regex(r"^cb_(pause|resume|skip|stop|mute|unmute|queue|close)_(-?\d+)$"))
async def player_buttons(client, query: CallbackQuery):
    _, action, chat_str = query.data.split("_", 2)
    chat_id = int(chat_str)

    if action not in ("queue",) and config.AUTH_ONLY_ADMINS and query.from_user.id != config.OWNER_ID:
        try:
            member = await client.get_chat_member(chat_id, query.from_user.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await query.answer("Sirf admins ye button use kar sakte hain.", show_alert=True)
                return
        except Exception:  # noqa: BLE001
            await query.answer("Permission check nahi ho paya.", show_alert=True)
            return

    try:
        if action == "pause":
            await player_core.pause(chat_id)
            await query.answer("⏸ Paused")
            await query.message.edit_reply_markup(player_markup(chat_id, True))
        elif action == "resume":
            await player_core.resume(chat_id)
            await query.answer("▶️ Resumed")
            await query.message.edit_reply_markup(player_markup(chat_id))
        elif action == "skip":
            nxt = await player_core.skip(chat_id)
            await query.answer("⏭ Skipped")
            if nxt:
                await query.message.edit_text(_now_card(nxt.title, nxt.requested_by), reply_markup=player_markup(chat_id))
            else:
                await query.message.edit_text("⏭ Queue khali hai — video chat se nikal gaya.")
        elif action == "stop":
            await player_core.stop(chat_id)
            await query.answer("⏹ Stopped")
            await log_stop("Group", chat_id, query.from_user.first_name or "admin")
            await query.message.edit_text("⏹ Ruk gaya aur video chat chhod di.")
        elif action == "mute":
            await player_core.mute(chat_id)
            await query.answer("🔇 Muted")
        elif action == "unmute":
            await player_core.unmute(chat_id)
            await query.answer("🔊 Unmuted")
        elif action == "queue":
            state = get_state(chat_id)
            if not state.current:
                await query.answer("Abhi kuch play nahi ho raha.", show_alert=True)
            else:
                await query.answer(f"Now: {state.current.title[:60]}\nQueue: {len(state.queue)} tracks", show_alert=True)
        elif action == "close":
            await query.message.delete()
    except Exception as e:  # noqa: BLE001
        logger.warning("Button %s failed in %s: %s", action, chat_id, e)
        await query.answer(str(e)[:190] or "Error", show_alert=True)


_register_private_hint(
    [
        "vplay", "play", "vpause", "vresume", "vskip", "vstop", "vend",
        "vseek", "vqueue", "vplaylist", "vnowplaying", "vnp", "vcurrent",
        "vmute", "vunmute",
    ]
)
