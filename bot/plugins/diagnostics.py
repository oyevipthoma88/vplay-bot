"""Logs every command the bot receives (so the Heroku logs show whether
Telegram is delivering updates at all) and provides /vdebug.
"""
from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import Message

from bot.core import runtime
from bot.core.clients import assistant, bot
from bot.logger import logger
from bot.utils.commands import cmd, mark_seen
from bot.utils.decorators import error_guard


@bot.on_message(filters.all, group=-1)
async def _log_incoming(client, message: Message):
    text = (message.text or message.caption or "").strip()
    if not text or text[0] not in "/!":
        return
    mark_seen(message)
    user = message.from_user
    logger.info(
        "CMD from %s (%s) in chat %s [%s]: %s",
        (user.first_name if user else getattr(message.sender_chat, "title", "?")),
        (user.id if user else "-"),
        message.chat.id if message.chat else "?",
        getattr(message.chat.type, "name", message.chat.type) if message.chat else "?",
        text[:200],
    )


async def _status_of(client, chat_id, user_id):
    try:
        m = await client.get_chat_member(chat_id, user_id)
        return getattr(m.status, "name", str(m.status)), m
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}", None


@bot.on_message(cmd("vdebug"))
@error_guard
async def vdebug_cmd(client, message: Message):
    lines = [
        "🩺 <b>VPlay debug</b>",
        f"Bot: @{runtime.BOT_USERNAME} (id {runtime.BOT_ID})",
        f"Assistant: {runtime.ASSISTANT_NAME} (id {runtime.ASSISTANT_ID})",
        f"Chat: {message.chat.id} ({getattr(message.chat.type, 'name', '?')})",
    ]
    if message.chat and message.chat.type.name in ("GROUP", "SUPERGROUP"):
        bot_status, bot_member = await _status_of(client, message.chat.id, "me")
        lines.append(f"Bot status: {bot_status}")
        if bot_member and bot_member.status == ChatMemberStatus.ADMINISTRATOR:
            priv = bot_member.privileges
            lines.append(
                f"  invite users: {getattr(priv, 'can_invite_users', '?')}, "
                f"manage video chats: {getattr(priv, 'can_manage_video_chats', '?')}"
            )
        a_status, a_member = await _status_of(assistant, message.chat.id, "me")
        lines.append(f"Assistant status: {a_status}")
        if a_member and a_member.status == ChatMemberStatus.ADMINISTRATOR:
            priv = a_member.privileges
            lines.append(
                f"  manage video chats: {getattr(priv, 'can_manage_video_chats', '?')}"
            )
        try:
            from pyrogram import raw
            peer = await assistant.resolve_peer(message.chat.id)
            if isinstance(peer, raw.types.InputPeerChannel):
                full = await assistant.invoke(raw.functions.channels.GetFullChannel(
                    channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)))
            else:
                full = await assistant.invoke(raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
            on = getattr(full.full_chat, "call", None) is not None
            lines.append(f"Video chat: {'✅ ON' if on else '❌ OFF'}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"Video chat: check fail ({type(e).__name__})")
        from bot.core.queue import get_state
        st = get_state(message.chat.id)
        lines.append(f"Player: {'▶️ ' + st.current.title if st.current else 'idle'} · queue {len(st.queue)}")
    await message.reply_text("\n".join(lines))
