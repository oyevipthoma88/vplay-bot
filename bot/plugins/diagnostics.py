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
    def _check(v) -> str:
        return "✅" if v else "❌"

    def _perm(priv, attr) -> str:
        return _check(getattr(priv, attr, False))

    lines = [
        "🩺 <b>VPlay Debug</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🤖 <b>Bot</b>",
        f"   @{runtime.BOT_USERNAME}",
        f"   <code>id {runtime.BOT_ID}</code>",
        "",
        "🎙 <b>Assistant</b>",
        f"   {runtime.ASSISTANT_NAME}",
        f"   <code>id {runtime.ASSISTANT_ID}</code>",
        "",
        "💬 <b>Chat</b>",
        f"   <code>{message.chat.id}</code>",
        f"   {getattr(message.chat.type, 'name', '?')}",
    ]

    is_group = (
        message.chat
        and getattr(message.chat, "type", None)
        and message.chat.type.name in ("GROUP", "SUPERGROUP")
    )

    if is_group:
        # ---- Permissions ----
        lines.append("")
        lines.append("🔐 <b>Permissions</b>")

        bot_status, bot_member = await _status_of(client, message.chat.id, "me")
        _bot_admin = bool(bot_member and bot_member.status == ChatMemberStatus.ADMINISTRATOR)
        lines.append(f"   Bot: {_check(_bot_admin)} {bot_status}")
        if _bot_admin:
            _priv = bot_member.privileges
            lines.append(
                f"   ├ Invite: {_perm(_priv, 'can_invite_users')}  "
                f"Manage VC: {_perm(_priv, 'can_manage_video_chats')}"
            )

        a_status, a_member = await _status_of(assistant, message.chat.id, "me")
        _a_admin = bool(a_member and a_member.status == ChatMemberStatus.ADMINISTRATOR)
        lines.append(f"   Assistant: {_check(_a_admin)} {a_status}")
        if _a_admin:
            _priv = a_member.privileges
            lines.append(f"   └ Manage VC: {_perm(_priv, 'can_manage_video_chats')}")

        # ---- Video chat ----
        lines.append("")
        lines.append("📞 <b>Video Chat</b>")
        try:
            from pyrogram import raw
            peer = await assistant.resolve_peer(message.chat.id)
            if isinstance(peer, raw.types.InputPeerChannel):
                full = await assistant.invoke(raw.functions.channels.GetFullChannel(
                    channel=raw.types.InputChannel(
                        channel_id=peer.channel_id, access_hash=peer.access_hash)))
            else:
                full = await assistant.invoke(
                    raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
            on = getattr(full.full_chat, "call", None) is not None
            lines.append(f"   {_check(on)} {'ON' if on else 'OFF'}")
            if not on:
                lines.append("   <i>→ Video chat start karein, ya assistant ko admin banayein</i>")
        except Exception as e:  # noqa: BLE001
            lines.append(f"   ⚠️ Check fail (<code>{type(e).__name__}</code>)")

        # ---- Player ----
        lines.append("")
        lines.append("🎬 <b>Player</b>")
        try:
            from bot.core.queue import get_state
            from bot.utils.formatting import format_duration
            st = get_state(message.chat.id)
            if st.current:
                _title = st.current.title
                if len(_title) > 40:
                    _title = _title[:37] + "…"
                lines.append(f"   ▶️ {_title}")
                _el = st.elapsed()
                _dur = st.current.duration
                if _dur:
                    lines.append(
                        f"   <code>{format_duration(_el)} / {format_duration(_dur)}</code>"
                    )
                _stat = ["⏸ Paused" if st.is_paused else "▶️ Playing"]
                if st.is_muted:
                    _stat.append("🔇 Muted")
                _lm = getattr(st, "loop_mode", "off")
                if _lm != "off":
                    _stat.append(f"🔁 {_lm.title()}")
                _vol = getattr(st, "volume", 100)
                if _vol != 100:
                    _stat.append(f"🔊 {_vol}%")
                lines.append(f"   {'  ·  '.join(_stat)}")
                lines.append(f"   Queue: <code>{len(st.queue)}</code>")
            else:
                lines.append("   <i>idle (kuch play nahi ho raha)</i>")
        except Exception as e:  # noqa: BLE001
            lines.append(f"   ⚠️ State fail (<code>{type(e).__name__}</code>)")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    await message.reply_text("\n".join(lines))
