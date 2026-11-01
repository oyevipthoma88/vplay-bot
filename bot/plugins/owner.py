"""Owner panel — broadcast, stats, start pic, default thumb, maintenance,
log group, restart. Works in private AND groups; never replies silently.
State persists via bot.database.owner_data (Mongo + in-memory fallback).
"""
import asyncio

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import CallbackQuery, Message

from bot import __version__, config
from bot.core.clients import bot
from bot.core.queue import active_chats
from bot.database import get_served_chats, get_stats, set_log_channel
from bot.database.owner_data import (
    get_default_thumb, get_start_pic, is_maintenance,
    set_default_thumb, set_maintenance, set_start_pic,
)
from bot.logger import logger
from bot.utils import ui
from bot.utils.commands import cmd
from bot.utils.decorators import error_guard

_AWAITING: dict = {}
OWNER_COMMANDS = ["owner", "panel", "admin", "ownersettings"]


def _is_owner(user_id: int) -> bool:
    return bool(user_id) and user_id == config.OWNER_ID


async def _safe_edit(message, text: str, markup=None) -> None:
    """Edit text or caption — never raises."""
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(caption=text, reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception as e:  # noqa: BLE001
        logger.info("owner _safe_edit fail: %s", e)


@bot.on_message(cmd(OWNER_COMMANDS))
@error_guard
async def owner_cmd(client, message: Message):
    _uid = message.from_user.id if message.from_user else 0
    logger.info(
        "owner_cmd: chat=%s type=%s uid=%s owner=%s",
        message.chat.id, getattr(message.chat.type, "name", "?"),
        _uid, config.OWNER_ID,
    )
    if not _is_owner(_uid):
        await message.reply_text(
            "🔒 <b>Owner panel</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>Aapka ID:</b> <code>{_uid}</code>\n"
            f"👑 <b>Owner ID:</b> <code>{config.OWNER_ID}</code>\n\n"
            "<i>Owner hain? Heroku config me <code>OWNER_ID</code> check karein.</i>",
            **ui.NO_PREVIEW,
        )
        return
    await message.reply_text(
        ui.owner_panel_text(),
        reply_markup=ui.owner_panel_markup(),
        **ui.HTML, **ui.NO_PREVIEW,
    )


@bot.on_message(filters.private & filters.regex(r"^/start\s+(owner|panel|admin)$"))
async def owner_deeplink(client, message: Message):
    await owner_cmd(client, message)


@bot.on_callback_query(filters.regex(r"^own_"))
async def owner_buttons(client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        await query.answer("🔒 Owner only", show_alert=True)
        return
    action = query.data[4:]
    try:
        if action == "close":
            await query.answer()
            try:
                await query.message.delete()
            except Exception:  # noqa: BLE001
                pass
            return

        if action == "refresh":
            await query.answer("🔄 Updated")
            await _safe_edit(query.message, ui.owner_panel_text(), ui.owner_panel_markup())
            return

        if action == "stats":
            await query.answer("📊 Loading…")
            try:
                s = await get_stats()
                _db = "✅ Mongo" if s.get("is_mongo") else "⚠️ Memory"
                _c = s.get("total_chats", 0)
                _u = s.get("total_users", 0)
                _p = s.get("total_plays", 0)
            except Exception as e:  # noqa: BLE001
                logger.warning("owner stats fail: %s", e)
                _db, _c, _u, _p = "⚠️ DB error (quota?)", 0, 0, 0
            text = (
                "📊 <b>Bot Stats</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🗄 <b>DB:</b> {_db}\n"
                f"👥 <b>Chats:</b> <code>{_c}</code>\n"
                f"👤 <b>Users:</b> <code>{_u}</code>\n"
                f"🎬 <b>Plays:</b> <code>{_p}</code>\n"
                f"📡 <b>Active calls:</b> <code>{active_chats()}</code>\n"
                f"📦 <i>v{__version__}</i>"
            )
            await _safe_edit(query.message, text, ui.owner_panel_markup())
            return

        if action == "maintenance":
            new_state = not is_maintenance()
            set_maintenance(new_state)
            await query.answer(
                "⚙️ Maintenance ON — non-owners paused" if new_state
                else "⚪ Maintenance OFF — bot active",
                show_alert=True,
            )
            await _safe_edit(query.message, ui.owner_panel_text(), ui.owner_panel_markup())
            return

        if action == "start_pic":
            _AWAITING[query.from_user.id] = "start_pic"
            await query.answer("🖼 Photo bhejo", show_alert=True)
            await query.message.reply_text(
                "🖼 <b>Start Pic</b>\n\nEk photo bhejo — /start par dikhegi.\n"
                "/cancel se cancel karo."
            )
            return

        if action == "start_pic_clear":
            set_start_pic(None)
            await query.answer("🗑 Start pic removed", show_alert=True)
            await _safe_edit(query.message, ui.owner_panel_text(), ui.owner_panel_markup())
            return

        if action == "thumb":
            _AWAITING[query.from_user.id] = "thumb"
            await query.answer("🎬 Video/photo bhejo", show_alert=True)
            await query.message.reply_text(
                "🎬 <b>Default Thumbnail</b>\n\nVideo ya photo bhejo — jis track ka "
                "thumbnail nahi hoga uspe ye lagega.\n/cancel se cancel karo."
            )
            return

        if action == "log_group":
            _AWAITING[query.from_user.id] = "log_group"
            await query.answer("📝 Chat ID bhejo", show_alert=True)
            await query.message.reply_text(
                "📝 <b>Log Group</b>\n\nChat ID bhejo (jaise "
                "<code>-100123456789</code>). Bot ko wahan admin banayein.\n"
                "/cancel se cancel karo."
            )
            return

        if action == "broadcast":
            _AWAITING[query.from_user.id] = "broadcast"
            await query.answer("📢 Message bhejo", show_alert=True)
            await query.message.reply_text(
                "📢 <b>Broadcast</b>\n\nJo message bhejna hai wo bhejo "
                "(text/photo/video).\n/cancel se cancel karo."
            )
            return

        if action == "restart":
            await query.answer()
            await _safe_edit(
                query.message,
                "🔄 <b>Restart Bot</b>\n\n"
                "Heroku dashboard → app → <b>More → Restart all dynos</b>\n\n"
                "<i>Ya CLI:</i>\n"
                "<code>heroku ps:scale worker=0 -a APP</code>\n"
                "<code>heroku ps:scale worker=1 -a APP</code>",
                ui.owner_panel_markup(),
            )
            return

        await query.answer()
    except Exception as e:  # noqa: BLE001
        logger.exception("owner button %s failed", action)
        try:
            await query.answer(f"⚠️ {type(e).__name__}", show_alert=True)
        except Exception:  # noqa: BLE001
            pass


@bot.on_message(
    filters.private & (filters.text | filters.photo | filters.video), group=10
)
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
            _thumbs = getattr(message.video, "thumbs", None)
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
                _t = getattr(message.video, "thumbnail", None) or getattr(message.video, "thumb", None)
                if _t and getattr(_t, "file_id", None):
                    _thumb_id = _t.file_id
        if not _thumb_id:
            await message.reply_text("⚠️ Photo ya video bhejo. /cancel se cancel karo.")
            return
        set_default_thumb(_thumb_id)
        _AWAITING.pop(message.from_user.id, None)
        await message.reply_text("✅ Default thumbnail set!")
        return

    if state == "start_pic":
        if not message.photo:
            await message.reply_text("⚠️ Photo bhejo. /cancel se cancel karo.")
            return
        set_start_pic(message.photo.file_id)
        _AWAITING.pop(message.from_user.id, None)
        await message.reply_text("✅ Start pic set! /start me dikhegi.")
        return

    if state == "log_group":
        try:
            chat_id = int(text)
        except ValueError:
            await message.reply_text("⚠️ Number hona chahiye. /cancel ya dobara bhejo.")
            return
        try:
            await bot.send_message(chat_id, "✅ VPlay log group connected.")
        except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"⚠️ Chats fetch fail: <code>{type(e).__name__}</code>")
        return
    total = len(chats)
    if total == 0:
        await status.edit_text(
            "⚠️ <b>0 chats</b> — MongoDB quota full ho sakta hai. Atlas → "
            "Browse Collections → quota free karein."
        )
        return
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
            except Exception:  # noqa: BLE001
                failed += 1
        except Exception:  # noqa: BLE001
            failed += 1
        if i % 10 == 0 or i == total:
            try:
                await status.edit_text(
                    f"📢 <b>Broadcasting…</b>\n"
                    f"✅ Sent: <code>{success}</code>\n"
                    f"❌ Failed: <code>{failed}</code>\n"
                    f"📊 Progress: <code>{i}/{total}</code>"
                )
            except Exception:  # noqa: BLE001
                pass
        if i % 20 == 0:
            await asyncio.sleep(1)
    await status.edit_text(
        f"✅ <b>Broadcast complete</b>\n"
        f"📊 Total: <code>{total}</code>\n"
        f"✅ Sent: <code>{success}</code>\n"
        f"❌ Failed: <code>{failed}</code>"
    )
