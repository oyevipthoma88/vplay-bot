import functools

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import Message

from bot import config
from bot.logger import logger


async def _try_reply(message: Message, text: str) -> None:
    """Replying can itself fail (deleted message, lost permissions). Never
    let that turn into a second, noisier crash.
    """
    try:
        await message.reply_text(text)
    except Exception:  # noqa: BLE001
        logger.warning("Could not send reply in chat %s", getattr(message.chat, "id", "?"))


def error_guard(func):
    """Turns any exception raised by a handler into a friendly chat reply
    instead of a silently-dropped update / a crash in the logs only.
    """

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        try:
            return await func(client, message, *args, **kwargs)
        except (ValueError, RuntimeError) as e:
            await _try_reply(message, str(e) or "Kuch galat ho gaya.")
        except Exception as e:  # noqa: BLE001 - last line of defense
            logger.exception("Handler error in %s", func.__name__)
            await _try_reply(message, f"Kuch error aa gaya: {e}")

    return wrapper


async def _is_authorised(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id == config.OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except Exception:  # noqa: BLE001 - not a member / can't check => not allowed
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def admin_only(func):
    """Restricts a command to group admins + OWNER_ID, unless
    AUTH_ONLY_ADMINS=false. Always allowed in private chats.
    """

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        if not config.AUTH_ONLY_ADMINS:
            return await func(client, message, *args, **kwargs)
        if message.chat and message.chat.type == ChatType.PRIVATE:
            return await func(client, message, *args, **kwargs)
        if not message.from_user:
            # Anonymous admins / channel-signed posts: the sender is hidden,
            # so we can't verify them. Treat as an admin only if the message
            # comes from the chat itself (anonymous admin posts do).
            if getattr(message, "sender_chat", None):
                return await func(client, message, *args, **kwargs)
            await _try_reply(message, "Sirf group admins ye command use kar sakte hain.")
            return
        allowed = await _is_authorised(client, message.chat.id, message.from_user.id)
        if not allowed:
            await _try_reply(message, "Sirf group admins ye command use kar sakte hain.")
            return
        return await func(client, message, *args, **kwargs)

    return wrapper
