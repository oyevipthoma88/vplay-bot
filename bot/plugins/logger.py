"""Deep logging plugin — sends events to LOG_CHANNEL_ID."""
import html
import time

from pyrogram import filters
from pyrogram.types import Message

from bot.core.clients import bot
from bot.core.log_helpers import send_log_card

_recent = {}


def _should_log(key: tuple) -> bool:
    now = time.time()
    if key in _recent and now - _recent[key] < 3:
        return False
    _recent[key] = now
    if len(_recent) > 500:
        for k in list(_recent.keys()):
            if now - _recent[k] > 60:
                del _recent[k]
    return True


def _fmt_user(user) -> str:
    if not user:
        return "<i>unknown</i>"
    s = html.escape(user.first_name or "?")
    if getattr(user, "username", None):
        s += f" (@{user.username})"
    s += f" <code>{user.id}</code>"
    return s


def _fmt_chat(chat) -> str:
    if not chat:
        return "?"
    ct = getattr(chat.type, "name", "?") if chat.type else "?"
    if ct == "PRIVATE":
        return f"<i>Private</i> <code>{chat.id}</code>"
    title = html.escape(chat.title or "?")
    return f"{title} <code>{chat.id}</code> [{ct}]"


@bot.on_message(filters.all, group=-2)
async def _log_command(client, message: Message):
    text = (message.text or message.caption or "").strip()
    if not text.startswith(("/", "!")):
        return
    cmd_name = text.split()[0][:50]
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else 0
    if not _should_log((user_id, chat_id, cmd_name)):
        return
    await send_log_card(
        "Command Received",
        [
            f"👤 <b>User:</b> {_fmt_user(message.from_user)}",
            f"💬 <b>Chat:</b> {_fmt_chat(message.chat)}",
            f"⚡ <b>Command:</b> <code>{html.escape(text[:150])}</code>",
            f"🕐 <code>{time.strftime('%H:%M:%S')} UTC</code>",
        ],
        emoji="📥",
    )


@bot.on_message(filters.new_chat_members, group=-1)
async def _log_bot_added(client, message: Message):
    if not client.me:
        return
    if not any(getattr(m, "id", 0) == client.me.id for m in message.new_chat_members):
        return
    await send_log_card(
        "Bot Added to Group",
        [
            f"👥 <b>Group:</b> {_fmt_chat(message.chat)}",
            f"👤 <b>Added by:</b> {_fmt_user(message.from_user)}",
        ],
        emoji="➕",
    )


@bot.on_message(filters.left_chat_member, group=-1)
async def _log_bot_removed(client, message: Message):
    if not client.me:
        return
    if getattr(message.left_chat_member, "id", 0) != client.me.id:
        return
    await send_log_card(
        "Bot Removed from Group",
        [
            f"👥 <b>Group:</b> {_fmt_chat(message.chat)}",
            f"👤 <b>Removed by:</b> {_fmt_user(message.from_user)}",
        ],
        emoji="➖",
    )
