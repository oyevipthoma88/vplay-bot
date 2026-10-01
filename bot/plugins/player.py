from pyrogram import filters
from pyrogram.types import Message

from bot.core import player as player_core
from bot.core.clients import bot
from bot.core.queue import Track, get_state
from bot.utils.commands import cmd
from bot.utils.decorators import admin_only, error_guard
from bot.logger import logger
from bot.utils.extract import resolve_source
from bot.utils.formatting import format_duration, parse_time_to_seconds

# Playback only makes sense inside a group's video chat. In private chats we
# still answer, so the command doesn't just vanish into silence.
_GROUP = filters.group
_PRIVATE = filters.private

_PRIVATE_HINT = (
    "Ye command sirf group ke andar kaam karta hai. Bot ko group me add "
    "karein, video chat start karein, aur wahan command bhejein."
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
    logger.info("vplay[%s]: start", chat_id)
    status = await message.reply_text("Media ready kar raha hoon...")
    try:
        await player_core.ensure_assistant_in_chat(chat_id)
        logger.info("vplay[%s]: assistant chat me hai", chat_id)
        source, title, is_url = await resolve_source(message)
        logger.info("vplay[%s]: source ready (%s) %s", chat_id, "link" if is_url else "file", title)
    except Exception:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        raise

    track = Track(
        source=source,
        title=title,
        requested_by=message.from_user.first_name if message.from_user else "someone",
        is_url=is_url,
    )
    try:
        started = await player_core.play_or_queue(message.chat.id, track)
    except Exception:
        if not is_url:
            player_core._cleanup_file(track)
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        raise
    logger.info("vplay[%s]: %s", chat_id, "playing" if started else "queued")
    if started:
        await status.edit_text(f"▶️ Ab chal raha hai: {title}")
    else:
        position = len(get_state(message.chat.id).queue)
        await status.edit_text(f"➕ Queue mein add ho gaya (position {position}): {title}")


@bot.on_message(cmd("vpause") & _GROUP)
@error_guard
@admin_only
async def vpause_cmd(client, message: Message):
    await player_core.pause(message.chat.id)
    await message.reply_text("⏸ Paused.")


@bot.on_message(cmd("vresume") & _GROUP)
@error_guard
@admin_only
async def vresume_cmd(client, message: Message):
    await player_core.resume(message.chat.id)
    await message.reply_text("▶️ Resumed.")


@bot.on_message(cmd("vskip") & _GROUP)
@error_guard
@admin_only
async def vskip_cmd(client, message: Message):
    if get_state(message.chat.id).current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    next_track = await player_core.skip(message.chat.id)
    if next_track:
        await message.reply_text(f"⏭ Skip ho gaya. Ab chal raha hai: {next_track.title}")
    else:
        await message.reply_text("⏭ Queue khali hai — video chat se nikal gaya.")


@bot.on_message(cmd(["vstop", "vend"]) & _GROUP)
@error_guard
@admin_only
async def vstop_cmd(client, message: Message):
    await player_core.stop(message.chat.id)
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
    status = await message.reply_text(f"Seek kar raha hoon {format_duration(seconds)} tak...")
    await player_core.seek(message.chat.id, seconds)
    await status.edit_text(f"⏩ Ab {format_duration(seconds)} se chal raha hai.")


@bot.on_message(cmd(["vqueue", "vplaylist"]) & _GROUP)
@error_guard
async def vqueue_cmd(client, message: Message):
    state = get_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    lines = [f"▶️ Now playing: {state.current.title}"]
    if state.queue:
        for i, track in enumerate(state.queue[:25], start=1):
            lines.append(f"{i}. {track.title}")
        if len(state.queue) > 25:
            lines.append(f"...aur {len(state.queue) - 25} aur.")
    else:
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
        f"{status}: {state.current.title}\n"
        f"Elapsed: {format_duration(state.elapsed())}\n"
        f"Requested by: {state.current.requested_by}"
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


_register_private_hint(
    [
        "vplay", "play", "vpause", "vresume", "vskip", "vstop", "vend",
        "vseek", "vqueue", "vplaylist", "vnowplaying", "vnp", "vcurrent",
        "vmute", "vunmute",
    ]
)
