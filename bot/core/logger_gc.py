"""Log channel/group (Log GC): play logs, stop logs and crash reports."""
import html
import logging

from bot.core.clients import bot
from bot.database import get_log_channel

# Kurigram 2.2+ deprecated `disable_web_page_preview`; use `link_preview_options`.
try:
    from pyrogram.types import LinkPreviewOptions as _LPO
    _NO_PREVIEW = {"link_preview_options": _LPO(is_disabled=True)}
except ImportError:
    _NO_PREVIEW = {"disable_web_page_preview": True}


logger = logging.getLogger(__name__)


async def send_log(text: str) -> None:
    channel_id = await get_log_channel()
    if not channel_id:
        return
    try:
        await bot.send_message(channel_id, text, **_NO_PREVIEW)
    except Exception as e:  # noqa: BLE001
        logger.warning("Log channel %s me message nahi gaya: %s", channel_id, e)


_MODES = {"stream": "0s MTProto stream", "link": "Direct link", "file": "Local file"}


async def log_play(chat_title, chat_id, user_name, user_id, title, mode) -> None:
    await send_log(
        "🎬 <b>#VPLAY</b>\n"
        f"📍 <b>Chat:</b> {html.escape(chat_title)} [<code>{chat_id}</code>]\n"
        f"👤 <b>User:</b> {html.escape(user_name)} [<code>{user_id}</code>]\n"
        f"🎞 <b>Media:</b> <i>{html.escape(title)}</i>\n"
        f"⚡ <b>Mode:</b> {_MODES.get(mode, mode)}"
    )


async def log_stop(chat_title, chat_id, user_name) -> None:
    await send_log(
        "⏹ <b>#VSTOP</b>\n"
        f"📍 <b>Chat:</b> {html.escape(chat_title)} [<code>{chat_id}</code>]\n"
        f"👤 <b>By:</b> {html.escape(user_name)}"
    )


async def log_error(chat_id, command, error, tb) -> None:
    await send_log(
        "🚨 <b>#ERROR</b>\n"
        f"📍 <b>Chat:</b> <code>{chat_id}</code>\n"
        f"⚙️ <b>Handler:</b> <code>{command}</code>\n"
        f"❌ <b>Error:</b> <code>{html.escape(str(error))[:500]}</code>\n\n"
        f"<pre>{html.escape(tb[-1500:])}</pre>"
    )
