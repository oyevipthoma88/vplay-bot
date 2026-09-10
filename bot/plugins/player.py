from pyrogram import filters
from pyrogram.types import Message

from bot.core import player as player_core
from bot.core.clients import bot
from bot.core.queue import Track, get_state
from bot.utils.decorators import admin_only, error_guard
from bot.utils.extract import resolve_source
from bot.utils.formatting import format_duration, parse_time_to_seconds


@bot.on_message(filters.command(["vplay", "play"]) & filters.group)
@error_guard
@admin_only
async def vplay_cmd(client, message: Message):
    status = await message.reply_text("Media ready kar raha hoon...")
    source, title, is_url = await resolve_source(message)
    track = Track(
        source=source,
        title=title,
        requested_by=message.from_user.first_name if message.from_user else "someone",
        is_url=is_url,
    )
    started = await player_core.play_or_queue(message.chat.id, track)
    if started:
        await status.edit_text(f"▶️ Ab chal raha hai: {title}")
    else:
        position = len(get_state(message.chat.id).queue)
        await status.edit_text(f"➕ Queue mein add ho gaya (position {position}): {title}")


@bot.on_message(filters.command("vpause") & filters.group)
@error_guard
@admin_only
async def vpause_cmd(client, message: Message):
    await player_core.pause(message.chat.id)
    await message.reply_text("⏸ Paused.")


@bot.on_message(filters.command("vresume") & filters.group)
@error_guard
@admin_only
async def vresume_cmd(client, message: Message):
    await player_core.resume(message.chat.id)
    await message.reply_text("▶️ Resumed.")


@bot.on_message(filters.command("vskip") & filters.group)
@error_guard
@admin_only
async def vskip_cmd(client, message: Message):
    next_track = await player_core.skip(message.chat.id)
    if next_track:
        await message.reply_text(f"⏭ Skip ho gaya. Ab chal raha hai: {next_track.title}")
    else:
        await message.reply_text("⏭ Queue khali hai — video chat se nikal gaya.")


@bot.on_message(filters.command(["vstop", "vend"]) & filters.group)
@error_guard
@admin_only
async def vstop_cmd(client, message: Message):
    await player_core.stop(message.chat.id)
    await message.reply_text("⏹ Ruk gaya aur video chat chhod di.")


@bot.on_message(filters.command("vseek") & filters.group)
@error_guard
@admin_only
async def vseek_cmd(client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: /vseek <seconds ya mm:ss>")
        return
    try:
        seconds = parse_time_to_seconds(parts[1])
    except ValueError:
        await message.reply_text("Time samajh nahi aaya. Example: /vseek 90 ya /vseek 1:30")
        return
    status = await message.reply_text(f"Seek kar raha hoon {format_duration(seconds)} tak...")
    await player_core.seek(message.chat.id, seconds)
    await status.edit_text(f"⏩ Ab {format_duration(seconds)} se chal raha hai.")


@bot.on_message(filters.command(["vqueue", "vplaylist"]) & filters.group)
@error_guard
async def vqueue_cmd(client, message: Message):
    state = get_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    lines = [f"▶️ Now playing: {state.current.title}"]
    if state.queue:
        for i, track in enumerate(state.queue, start=1):
            lines.append(f"{i}. {track.title}")
    else:
        lines.append("(queue khali hai)")
    await message.reply_text("\n".join(lines))


@bot.on_message(filters.command(["vnowplaying", "vnp", "vcurrent"]) & filters.group)
@error_guard
async def vnowplaying_cmd(client, message: Message):
    state = get_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Abhi kuch play nahi ho raha.")
        return
    await message.reply_text(
        f"▶️ {state.current.title}\nElapsed: {format_duration(state.elapsed())}"
    )


@bot.on_message(filters.command("vmute") & filters.group)
@error_guard
@admin_only
async def vmute_cmd(client, message: Message):
    await player_core.mute(message.chat.id)
    await message.reply_text("🔇 Muted.")


@bot.on_message(filters.command("vunmute") & filters.group)
@error_guard
@admin_only
async def vunmute_cmd(client, message: Message):
    await player_core.unmute(message.chat.id)
    await message.reply_text("🔊 Unmuted.")
