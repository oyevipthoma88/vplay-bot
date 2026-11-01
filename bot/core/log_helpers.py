"""Shared logging helpers for log channel."""
import asyncio
import time

from pyrogram.errors import FloodWait

from bot import config
from bot.core.clients import bot
from bot.database import get_log_channel
from bot.logger import logger

_last_send = 0.0


async def _log_chat_id():
    try:
        cid = await get_log_channel()
    except Exception:
        cid = None
    return cid or config.LOG_CHANNEL_ID


async def send_log_card(title: str, lines: list, emoji: str = "📋") -> None:
    """Send a box-drawn log card to LOG_CHANNEL_ID (rate-limited)."""
    global _last_send
    cid = await _log_chat_id()
    if not cid:
        return
    text = f"{emoji} <b>{title}</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)

    wait = 0.4 - (time.time() - _last_send)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_send = time.time()

    for attempt in (1, 2):
        try:
            await bot.send_message(cid, text, disable_web_page_preview=True)
            return
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            if attempt == 2:
                logger.warning("send_log_card: flood retry failed")
        except Exception as e:
            logger.warning("send_log_card failed: %s", e)
            return
