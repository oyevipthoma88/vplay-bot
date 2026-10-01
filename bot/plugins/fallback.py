"""Assistant fallback listener.

The assistant is a normal user account, so it sees every message in groups
it's in. If the bot does NOT receive a command (bot not in the group, bot
privacy mode, another bot swallowing /play, ...), the assistant notices
after a short delay and runs the same handler, so commands never vanish
silently. Disable with ASSISTANT_FALLBACK=false.
"""
import asyncio
import re

from pyrogram import filters
from pyrogram.types import Message

from bot import config
from bot.core import runtime
from bot.core.clients import assistant, bot
from bot.logger import logger
from bot.plugins import basic, diagnostics, player
from bot.utils.commands import is_seen, mark_seen

_CMD_RE = re.compile(r"^[/!]([A-Za-z_]+)(?:@(\w+))?(?:\s|$)")

COMMANDS = {
    "help": basic.help_cmd,
    "ping": basic.ping_cmd,
    "vdebug": diagnostics.vdebug_cmd,
    "vplay": player.vplay_cmd,
    "play": player.vplay_cmd,
    "vpause": player.vpause_cmd,
    "vresume": player.vresume_cmd,
    "vskip": player.vskip_cmd,
    "vstop": player.vstop_cmd,
    "vend": player.vstop_cmd,
    "vseek": player.vseek_cmd,
    "vqueue": player.vqueue_cmd,
    "vplaylist": player.vqueue_cmd,
    "vnowplaying": player.vnowplaying_cmd,
    "vnp": player.vnowplaying_cmd,
    "vcurrent": player.vnowplaying_cmd,
    "vmute": player.vmute_cmd,
    "vunmute": player.vunmute_cmd,
}

_WAIT_SECONDS = 4


if config.ASSISTANT_FALLBACK:

    @assistant.on_message(filters.group & (filters.text | filters.caption), group=50)
    async def _assistant_fallback(client, message: Message):
        text = (message.text or message.caption or "").strip()
        m = _CMD_RE.match(text)
        if not m:
            return
        command, target = m.group(1).lower(), m.group(2)
        if target and runtime.BOT_USERNAME and target.lower() != runtime.BOT_USERNAME.lower():
            return
        handler = COMMANDS.get(command)
        if handler is None:
            return

        # Only help out in chats where OUR bot is actually a member. The
        # assistant usually sits in many groups that use other music bots —
        # answering their /play would hijack them.
        try:
            await bot.get_chat_member(message.chat.id, "me")
        except Exception:  # noqa: BLE001
            return

        await asyncio.sleep(_WAIT_SECONDS)
        if is_seen(message) or not mark_seen(message):
            return  # the bot handled it

        logger.warning(
            "Bot ko '/%s' (chat %s) nahi mila — assistant fallback se chala raha hoon. "
            "Check karein: bot group me hai? BotFather me /setprivacy -> Disable?",
            command, message.chat.id,
        )

        run_client, run_message = assistant, message  # noqa: F841
        # Message ids are only shared between accounts in supergroups.
        is_super = getattr(message.chat.type, "name", "") == "SUPERGROUP"
        try:
            if not is_super:
                raise RuntimeError("basic group")
            fetched = await bot.get_messages(message.chat.id, message.id)
            if fetched and not getattr(fetched, "empty", False) and fetched.chat:
                run_client, run_message = bot, fetched
        except Exception as e:  # noqa: BLE001 - bot may not be in the chat
            logger.info("Bot message fetch nahi kar paya (%s) — assistant se reply hoga", e)

        try:
            await handler(run_client, run_message)
        except Exception:  # noqa: BLE001
            logger.exception("Fallback handler /%s failed", command)
