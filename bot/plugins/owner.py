"""Owner panel: broadcast, stats, start pic, maintenance, log, restart."""
import asyncio

from pyrogram import filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import CallbackQuery, Message

from bot import __version__, config
from bot.core.clients import bot
from bot.core.queue import active_chats
from bot.database import get_served_chats, get_stats, set_log_channel
from bot.database.owner_data import (
    get_start_pic, is_maintenance, set_maintenance, set_start_pic,
    set_default_thumb,
)
from bot.logger import logger
from bot.utils import ui
from bot.utils.commands import cmd
from bot.utils.decorators import error_guard

_AWAITING = {}


def _is_owner(user_id) -> bool:
    return user_id == config.OWNER_ID


@bot.on_message(cmd(["owner", "panel"]))
@error_guard
async def owner_cmd(client, message: Message):
    from pyrogram.enums import ChatType
    _uid = message.from_user.id if message.from_user else 0
    logger.info(
        "owner_cmd: chat=%s type=%s uid=%s expected_owner=%s",
        message.chat.id, getattr(message.chat.type, "name", "?"), _uid, config.OWNER_ID,
    )
    if _uid != config.OWNER_ID:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply_text(
                "🔒 <b>Owner-only panel</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Aapka ID:</b> <code>{_uid}</code>\n"
                f"👑 <b>Owner ID:</b> <code>{config.OWNER_ID}</code>\n\n"
                "<i>Agar aap owner hain to Heroku config me <code>OWNER_ID</code> "
                "check karein.</i>",
            )
        return
    await message.reply_text(
        ui.owner_panel_text(),
        reply_markup=ui.owner_panel_markup(),
        **ui.NO_PREVIEW,
    )


@bot.on_callback_query(filters.regex(r"^own_"))
async def owner_buttons(client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        await query.answer("🔒 Owner only", show_alert=True)
        return
    action = query.data[4:]
    try:
        if action == "close":
            await query.answer()
            await query.message.delete()
            return

        if action == "stats":
            s = await get_stats()
            text = (
                "📊 <b>Bot Stats</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🗄 DB: {'✅ Mongo' if s['is_mongo'] else '⚠️ Memory'}\n"
                f"👥 Chats: <code>{s['total_chats']}</code>\n"
                f"👤 Users: <code>{s['total_users']}</code>\n"
                f"🎬 Plays: <code>{s['total_plays']}</code>\n"
                f"📡 Active calls: <code>{active_chats()}</code>\n"
                f"📦 <i>v{__version__}</i>"
            )
            await query.answer()
            try:
                await query.message.edit_text(text, reply_markup=ui.owner_panel_markup())
            except MessageNotModified:
                pass
            return

        if action == "maintenance":
            new_state = not is_maintenance()
            set_maintenance(new_state)
            await query.answer(
                f"⚙️ Maintenance {'ON — bot non-owners ke liye paused' if new_state else 'OFF — bot active'}",
                show_alert=True,
            )
            try:
                await query.message.edit_text(
                    ui.owner_panel_text(),
                    reply_markup=ui.owner_panel_markup(),
                )
            except MessageNotModified:
                pass
            return

        if action == "start_pic":
            _AWAITING[query.from_user.id] = "start_pic"
            await query.answer("🖼 Photo bhejo", show_alert=True)
            await query.message.reply_text(
                "🖼 <b>Start Pic set karna</b>\n\n"
                "Ab ek photo bhejo. Wo <code>/start</code> par dikhegi.\n"
                "/cancel se cancel karo."
            )
            return

        if action == "thumb":
            _AWAITING[query.from_user.id] = "thumb"
            await query.answer("🎬 Video/photo bhejo", show_alert=True)
            await query.message.reply_text(
                "🎬 <b>Set Default Thumbnail</b>\n\n"
                "Ek video ya photo bhejo.\n"
                "• Video → uska thumbnail use hoga\n"
                "• Photo → wahi thumbnail banega\n\n"
                "/cancel se cancel karo."
            )
            return

        if action == "start_pic_clear":
            set_start_pic(None)
            await query.answer("🗑 Start pic removed", show_alert=True)
            try:
                await query.message.edit_text(
                    ui.owner_panel_text(),
                    reply_markup=ui.owner_panel_markup(),
                )
            except MessageNotModified:
                pass
            return

        if action == "log_group":
            _AWAITING[query.from_user.id] = "log_group"
            await query.answer("📝 Chat ID bhejo", show_alert=True)
            await query.message.reply_text(
                "📝 <b>Log Group</b>\n\n"
                "Log channel ka chat ID bhejo (jaise <code>-100123456789</code>).\n"
                "Bot ko wahan admin banayein.\n/cancel se cancel karo."
            )
            return

        if action == "broadcast":
            _AWAITING[query.from_user.id] = "broadcast"
            await query.answer("📢 Message bhejo", show_alert=True)
            await query.message.reply_text(
                "📢 <b>Broadcast</b>\n\n"
                "Jo message bhejna hai wo bhejo (text/photo/video).\n"
                "/cancel se cancel karo."
            )
            return

        if action == "restart":
            await query.answer()
            await query.message.edit_text(
                "🔄 <b>Restart Bot</b>\n\n"
                "Heroku dashboard → app → <b>More → Restart all dynos</b>\n\n"
                "<i>Ya CLI se:</i>\n"
                "<code>heroku ps:scale worker=0 -a APP</code>\n"
                "<code>sleep 5</code>\n"
                "<code>heroku ps:scale worker=1 -a APP</code>",
                reply_markup=ui.owner_panel_markup(),
            )
            return

        await query.answer()
    except Exception as e:
        logger.exception("owner button %s failed", action)
        await query.answer(f"⚠️ {type(e).__name__}", show_alert=True)


@bot.on_message(filters.private & (filters.text | filters.photo | filters.video), group=10)
async def _pending_input(client, message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    state = _AWAITING.get(message.from_user.id)
    if not state:
        return
    text = (message.text or "").strip()
    if text.lower() == "/cancel":
        _AWAITING.pop(message.from_user.id, None)
        await message.reply_text("❌ Cancelled.")
        return
    if text.startswith("/"):
        return

    if state == "thumb":
        _thumb_id = None
        if message.photo:
            _thumb_id = message.photo.file_id
        elif message.video:
            _vid = message.video
            _thumbs = getattr(_vid, "thumbs", None)
            if _thumbs:
                _sorted = sorted(
                    _thumbs,
                    key=lambda x: (getattr(x, "width", 0) or 0) * (getattr(x, "height", 0) or 0),
                    reverse=True,
                )
                for _t in _sorted:
                    if getattr(_t, "file_id", None):
                        _thumb_id = _t.file_id
                        break
            if not _thumb_id:
                _t = getattr(_vid, "thumbnail", None) or getattr(_vid, "thumb", None)
                if _t and getattr(_t, "file_id", None):
                    _thumb_id = _t.file_id
        if not _thumb_id:
            await message.reply_text("⚠️ Photo ya video bhejo. /cancel se cancel karo.")
            return
        set_default_thumb(_thumb_id)
        _AWAITING.pop(message.from_user.id, None)
        await message.reply_text("✅ Default thumbnail set! Ab jab track ka thumb nahi hoga, ye use hoga.")
        return

    if state == "start_pic":
        if not message.photo:
            await message.reply_text("⚠️ Photo bhejo. /cancel se cancel karo.")
            return
        set_start_pic(message.photo.file_id)
        _AWAITING.pop(message.from_user.id, None)
        await message.reply_text("✅ Start pic set! Ab /start me dikhegi.")
        return

    if state == "log_group":
        try:
            chat_id = int(text)
        except ValueError:
            await message.reply_text("⚠️ Number hona chahiye. /cancel ya dobara bhejo.")
            return
        try:
            await bot.send_message(chat_id, "✅ VPlay log group connected.")
        except Exception as e:
            await message.reply_text(f"⚠️ Bot nahi bhej paya: {e}")
            return
        await set_log_channel(chat_id)
        _AWAITING.pop(message.from_user.id, None)
        await message.reply_text(f"✅ Log group set: <code>{chat_id}</code>")
        return

    if state == "broadcast":
        _AWAITING.pop(message.from_user.id, None)
        await _do_broadcast(client, message)


async def _do_broadcast(client, message: Message):
    status = await message.reply_text("📢 <b>Fetching chats…</b>")
    try:
        chats = await get_served_chats()
    except Exception as e:
        await status.edit_text(f"⚠️ Chats fetch fail: {e}")
        return
    total = len(chats)
    success = failed = 0
    for i, chat_id in enumerate(chats, 1):
        try:
            await message.copy(chat_id, reply_markup=None)
            success += 1
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            try:
                await message.copy(chat_id, reply_markup=None)
                success += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        if i % 10 == 0 or i == total:
            try:
                await status.edit_text(
                    f"📢 <b>Broadcasting…</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Sent: <code>{success}</code>\n"
                    f"❌ Failed: <code>{failed}</code>\n"
                    f"📊 Progress: <code>{i}/{total}</code>"
                )
            except Exception:
                pass
        if i % 20 == 0:
            await asyncio.sleep(1)
    await status.edit_text(
        "✅ <b>Broadcast complete</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total: <code>{total}</code>\n"
        f"✅ Sent: <code>{success}</code>\n"
        f"❌ Failed: <code>{failed}</code>"
    )
