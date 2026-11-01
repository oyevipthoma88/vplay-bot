import time

from pyrogram import filters
from pyrogram.enums import ParseMode
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
        _is_owner = bool(message.from_user and message.from_user.id == config.OWNER_ID)
        # Deep-link: /start features
        _parts = (message.text or "").split(maxsplit=1)
        _payload = _parts[1].strip().lower() if len(_parts) > 1 else ""
        if _payload in ("owner", "panel", "admin"):
            return  # owner.py handles /start owner
        if _payload in ("features", "feature", "f"):
            await message.reply_text(
                ui.features_text(),
                reply_markup=ui.features_markup(),
                **ui.NO_PREVIEW,
            )
            return
        from bot.database.owner_data import get_start_pic
        _pic = get_start_pic()
        _text = ui.start_text(name)
        try:
            if _pic:
                await message.reply_photo(_pic, caption=_text,
                                          reply_markup=ui.start_markup(is_owner=_is_owner), **ui.HTML)
            else:
                await message.reply_text(_text, reply_markup=ui.start_markup(is_owner=_is_owner),
                                         **ui.NO_PREVIEW)
        except Exception as e:  # noqa: BLE001
            if not ui.is_owner_button_error(e):
                raise
            if _pic:
                await message.reply_photo(_pic, caption=_text,
                                          reply_markup=ui.start_markup(False, is_owner=_is_owner), **ui.HTML)
            else:
                await message.reply_text(_text, reply_markup=ui.start_markup(False, is_owner=_is_owner),
                                         **ui.NO_PREVIEW)
    else:
        await message.reply_text(ui.group_start_text(), reply_markup=ui.help_markup("main", False), **ui.HTML)


@bot.on_message(cmd("help"))
@error_guard
async def help_cmd(client, message: Message):
    private = message.chat.type == ChatType.PRIVATE
    await message.reply_text(ui.help_text("main"), reply_markup=ui.help_markup("main", private), **ui.HTML)


@bot.on_callback_query(filters.regex(r"^ui_(start|close|help_\w+|features|owner)$"))
async def ui_buttons(client, query: CallbackQuery):
    data = query.data[3:]

    async def _safe_edit(text, markup):
        try:
            if getattr(query.message, "photo", None):
                await query.message.edit_caption(caption=text, reply_markup=markup, parse_mode=ParseMode.HTML)
            else:
                await query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except MessageNotModified:
            pass
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await bot.send_message(query.message.chat.id, text, reply_markup=markup, **ui.HTML, **ui.NO_PREVIEW)

    try:
        if data == "close":
            await query.message.delete()
        elif data == "owner":
            if query.from_user.id != config.OWNER_ID:
                await query.answer("🔒 Owner only", show_alert=True)
                return
            await _safe_edit(ui.owner_panel_text(), ui.owner_panel_markup())
        elif data == "start":
            _is_owner = query.from_user.id == config.OWNER_ID
            await _safe_edit(ui.start_text(query.from_user.first_name),
                             ui.start_markup(is_owner=_is_owner))
        elif data == "features":
            await _safe_edit(ui.features_text(), ui.features_markup())
        else:
            page = data[len("help_"):]
            private = query.message.chat.type == ChatType.PRIVATE
            await _safe_edit(ui.help_text(page), ui.help_markup(page, private))
        await query.answer()
    except MessageNotModified:
        await query.answer()
    except Exception:
        await query.answer("Ye message ab edit nahi ho sakta.", show_alert=True)


@bot.on_message(cmd("id"))
@error_guard
async def id_cmd(client, message: Message):
    from pyrogram.enums import ChatType as _CT
    _u = message.from_user
    _uid = _u.id if _u else 0
    _uname = f"@{_u.username}" if _u and _u.username else "—"
    _name = _u.first_name if _u else "—"
    _lines = [
        "🆔 <b>Your Info</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Name:</b> {_name}",
        f"🔗 <b>Username:</b> {_uname}",
        f"🆔 <b>Your ID:</b> <code>{_uid}</code>",
        f"💬 <b>Chat ID:</b> <code>{message.chat.id}</code>",
        f"📂 <b>Type:</b> {getattr(message.chat.type, 'name', '?')}",
    ]
    if _uid == config.OWNER_ID:
        _lines.append("👑 <b>You are the bot owner ✅</b>")
    elif message.chat.type == _CT.PRIVATE:
        _lines.append(f"👑 <b>Owner ID:</b> <code>{config.OWNER_ID}</code>")
    await message.reply_text("\n".join(_lines), **ui.HTML)


@bot.on_message(cmd("ping"))
@error_guard
async def ping_cmd(client, message: Message):
    t0 = time.perf_counter()
    m = await message.reply_text("🏓 …")
    ms = (time.perf_counter() - t0) * 1000
    await m.edit_text(
        "🏓 <b>Pong!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Latency:</b> <code>{ms:.0f} ms</code>\n"
        f"⏱ <b>Uptime:</b> <code>{format_duration(int(time.time() - _BOOT))}</code>\n"
        f"🎬 <b>Active calls:</b> <code>{active_chats()}</code>\n"
        f"📦 <b>Version:</b> <i>VPlay v{__version__}</i>"
    )


@bot.on_message(cmd("stats"))
@error_guard
async def stats_cmd(client, message: Message):
    try:
        s = await get_stats()
    except Exception as e:  # noqa: BLE001
        await message.reply_text(
            "📊 <b>VPlay Stats</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ DB error: <code>{type(e).__name__}</code>\n"
            "<i>MongoDB Atlas quota full ho sakta hai.</i>"
        )
        return
    await message.reply_text(
        "📊 <b>VPlay Stats</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🗄 <b>Database:</b> {'✅ MongoDB' if s['is_mongo'] else '⚠️ In-memory'}\n"
        f"👥 <b>Chats:</b> <code>{s['total_chats']}</code>\n"
        f"👤 <b>Users:</b> <code>{s['total_users']}</code>\n"
        f"🎬 <b>Plays:</b> <code>{s['total_plays']}</code>\n"
        f"📡 <b>Active calls:</b> <code>{active_chats()}</code>"
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
