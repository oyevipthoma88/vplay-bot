import time

from pyrogram import filters
from pyrogram.enums import ChatType
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Message

from bot import __version__, config
from bot.core.clients import bot
from bot.core.queue import active_chats
from bot.database import add_served_user, get_stats, set_log_channel
from bot.utils import ui
from bot.utils.commands import cmd
from bot.utils.decorators import error_guard
from bot.utils.formatting import format_duration

_BOOT = time.time()


@bot.on_message(cmd("start"))
@error_guard
async def start_cmd(client, message: Message):
    if message.chat.type == ChatType.PRIVATE:
        if message.from_user:
            await add_served_user(message.from_user.id, message.from_user.first_name or "")
        name = message.from_user.first_name if message.from_user else ""
        try:
            await message.reply_text(ui.start_text(name), reply_markup=ui.start_markup(),
                                     disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            if not ui.is_owner_button_error(e):
                raise
            await message.reply_text(ui.start_text(name), reply_markup=ui.start_markup(False),
                                     disable_web_page_preview=True)
    else:
        await message.reply_text(ui.group_start_text(), reply_markup=ui.help_markup("main", False))


@bot.on_message(cmd("help"))
@error_guard
async def help_cmd(client, message: Message):
    private = message.chat.type == ChatType.PRIVATE
    await message.reply_text(ui.help_text("main"), reply_markup=ui.help_markup("main", private))


@bot.on_callback_query(filters.regex(r"^ui_(start|close|help_\w+)$"))
async def ui_buttons(client, query: CallbackQuery):
    data = query.data[3:]
    try:
        if data == "close":
            await query.message.delete()
        elif data == "start":
            try:
                await query.message.edit_text(ui.start_text(query.from_user.first_name),
                                              reply_markup=ui.start_markup(),
                                              disable_web_page_preview=True)
            except MessageNotModified:
                raise
            except Exception as e:  # noqa: BLE001
                if not ui.is_owner_button_error(e):
                    raise
                await query.message.edit_text(ui.start_text(query.from_user.first_name),
                                              reply_markup=ui.start_markup(False),
                                              disable_web_page_preview=True)
        else:
            page = data[len("help_"):]
            private = query.message.chat.type == ChatType.PRIVATE
            await query.message.edit_text(ui.help_text(page), reply_markup=ui.help_markup(page, private))
        await query.answer()
    except MessageNotModified:
        await query.answer()
    except Exception:  # noqa: BLE001
        await query.answer("Ye message ab edit nahi ho sakta.", show_alert=True)


@bot.on_message(cmd("ping"))
@error_guard
async def ping_cmd(client, message: Message):
    t0 = time.perf_counter()
    m = await message.reply_text("🏓 …")
    ms = (time.perf_counter() - t0) * 1000
    await m.edit_text(
        f"🏓 <b>Pong!</b> <code>{ms:.0f} ms</code>\n"
        f"⏱ Uptime: <code>{format_duration(int(time.time() - _BOOT))}</code>\n"
        f"🎬 Active calls: <code>{active_chats()}</code>\n"
        f"<i>VPlay v{__version__}</i>"
    )


@bot.on_message(cmd("stats"))
@error_guard
async def stats_cmd(client, message: Message):
    s = await get_stats()
    await message.reply_text(
        "📊 <b>VPlay Stats</b>\n\n"
        f"🗄 Database: {'✅ MongoDB' if s['is_mongo'] else '⚠️ In-memory (MONGO_DB_URI set karein)'}\n"
        f"👥 Chats: <code>{s['total_chats']}</code>\n"
        f"👤 Users: <code>{s['total_users']}</code>\n"
        f"🎬 Plays: <code>{s['total_plays']}</code>\n"
        f"📡 Active calls: <code>{active_chats()}</code>"
    )


@bot.on_message(cmd("setlog"))
@error_guard
async def setlog_cmd(client, message: Message):
    if not message.from_user or message.from_user.id != config.OWNER_ID:
        await message.reply_text("Sirf bot owner ye command use kar sakta hai.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: <code>/setlog -100123456789</code>")
        return
    try:
        channel_id = int(parts[1].strip())
    except ValueError:
        await message.reply_text("Chat ID number hona chahiye.")
        return
    try:
        await bot.send_message(channel_id, "✅ VPlay log group set ho gaya.")
    except Exception as e:  # noqa: BLE001
        await message.reply_text(f"⚠️ Bot us chat me message nahi bhej paya: {e}\nBot ko wahan admin banayein.")
        return
    await set_log_channel(channel_id)
    await message.reply_text(f"✅ Log group set: <code>{channel_id}</code>")
