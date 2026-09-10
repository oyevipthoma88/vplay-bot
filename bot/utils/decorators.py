import functools

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import Message

from bot import config
from bot.logger import logger


def error_guard(func):
    """Turns any exception raised by a handler into a friendly chat reply
    instead of a silently-dropped update / a crash in the logs only.
    """

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        try:
            await func(client, message, *args, **kwargs)
        except ValueError as e:
            await message.reply_text(str(e))
        except RuntimeError as e:
            await message.reply_text(str(e))
        except Exception as e:  # noqa: BLE001 - last line of defense
            logger.exception("Handler error in %s", func.__name__)
            await message.reply_text(f"Kuch error aa gaya: {e}")

    return wrapper


async def _is_authorised(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id == config.OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except Exception:
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
        if message.chat.type == ChatType.PRIVATE:
            return await func(client, message, *args, **kwargs)
        if not message.from_user:
            return await func(client, message, *args, **kwargs)
        allowed = await _is_authorised(client, message.chat.id, message.from_user.id)
        if not allowed:
            await message.reply_text("Sirf group admins ye command use kar sakte hain.")
            return
        return await func(client, message, *args, **kwargs)

    return wrapper
