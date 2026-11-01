import functools
import time
import traceback

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import ChatAdminRequired, FloodWait, PeerIdInvalid, UserNotParticipant
from pyrogram.types import Message

from bot import config
from bot.logger import logger


async def _try_reply(message: Message, text: str) -> None:
    chat_id = getattr(message.chat, "id", "?")
    try:
        await message.reply_text(text)
        logger.info("Reply in chat %s: %s", chat_id, text[:150].replace("\n", " "))
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not send reply in chat %s (%s): %s", chat_id, e, text[:100])


def _friendly(e: Exception) -> str:
    msg = str(e) or "Kuch galat ho gaya."
    low = msg.lower()
    if msg.startswith("⚠️"):
        return msg
    if "no active group call" in low or ("groupcall" in low and "not" in low):
        return "⚠️ Video chat shuru nahi hai. Pehle group me video chat start karein."
    if "usernotparticipant" in low:
        return "⚠️ Assistant group me nahi hai. Bot ko 'Invite users via link' permission dein."
    if "peer_id_invalid" in low:
        return "⚠️ Group resolve nahi hua. Ek baar /vdebug bhejein, phir dobara try karein."
    if ("over your space quota" in low or "writes are blocked" in low
            or "atlaserror" in low):
        return ("⚠️ Database (MongoDB Atlas) quota full hai — bot chalega par stats "
                "save nahi honge. Admin storage free karein.")
    if "processlookuperror" in low or "jsondecodeerror" in low:
        return "⚠️ Media file read nahi ho payi. Link/file check karein ya dobara try karein."
    if "ffmpeg" in low or "ffprobe" in low or "filenotfound" in low:
        return "⚠️ Server par ffmpeg/ffprobe missing ya file read fail. Admin ko batayein."
    if "timeout" in low or "timed out" in low:
        return "⚠️ Request timeout ho gaya. Thodi der baad dobara try karein."
    return f"⚠️ {msg[:300]}"


def error_guard(func):
    """Any handler exception -> friendly reply + full traceback in logs +
    crash report in the Log GC.
    """

    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        _t = time.time()
        try:
            from bot.utils.commands import mark_seen

            mark_seen(message)
        except Exception:  # noqa: BLE001
            pass
        try:
            return await func(client, message, *args, **kwargs)
        except (ValueError, RuntimeError) as e:
            logger.info("/%s -> user error: %s", func.__name__, e)
            await _try_reply(message, _friendly(e))
        except FloodWait as fw:
            await _try_reply(message, f"⏳ Telegram limit: {fw.value} second baad try karein.")
        except UserNotParticipant:
            await _try_reply(message, "⚠️ Assistant account group me nahi hai. Use add karein.")
        except ChatAdminRequired:
            await _try_reply(message, "⚠️ Bot/assistant ko admin permissions chahiye (Manage Video Chats).")
        except PeerIdInvalid:
            await _try_reply(message, "⚠️ Group abhi resolve nahi hua, kuch second baad dobara try karein.")
        except Exception as e:  # noqa: BLE001 - last line of defense
            logger.exception("Handler error in %s", func.__name__)
            try:
                from bot.core.logger_gc import log_error

                await log_error(getattr(message.chat, "id", 0), func.__name__, e, traceback.format_exc())
            except Exception:  # noqa: BLE001
                pass
            await _try_reply(message, f"⚠️ Kuch error aa gaya: {e}")
        finally:
            try:
                logger.info(
                    "handler[%s] chat=%s ms=%.0f",
                    func.__name__,
                    getattr(message.chat, "id", "?"),
                    (time.time() - _t) * 1000,
                )
            except Exception:  # noqa: BLE001
                pass

    return wrapper


async def _is_authorised(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id == config.OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Admin check fail (chat %s, user %s): %s", chat_id, user_id, e)
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


_linked_cache: dict = {}


async def _is_trusted_sender_chat(client: Client, chat, sender_chat) -> bool:
    if sender_chat.id == chat.id:
        return True  # anonymous group admin
    linked = _linked_cache.get(chat.id)
    if linked is None:
        try:
            full = await client.get_chat(chat.id)
            linked = getattr(getattr(full, "linked_chat", None), "id", 0) or 0
        except Exception as e:  # noqa: BLE001
            logger.warning("Linked channel check fail (chat %s): %s", chat.id, e)
            linked = 0
        _linked_cache[chat.id] = linked
    return bool(linked) and sender_chat.id == linked


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        if not config.AUTH_ONLY_ADMINS:
            return await func(client, message, *args, **kwargs)
        if message.chat and message.chat.type == ChatType.PRIVATE:
            return await func(client, message, *args, **kwargs)
        if not message.from_user:
            sender_chat = getattr(message, "sender_chat", None)
            # Only the group itself (= an anonymous admin) or the group's
            # linked channel may act without a user. Any member can "send as"
            # their OWN channel, so other sender_chats must not pass.
            if sender_chat and await _is_trusted_sender_chat(client, message.chat, sender_chat):
                return await func(client, message, *args, **kwargs)
            await _try_reply(message, "Sirf group admins ye command use kar sakte hain.")
            return
        if not await _is_authorised(client, message.chat.id, message.from_user.id):
            await _try_reply(message, "Sirf group admins ye command use kar sakte hain.")
            return
        return await func(client, message, *args, **kwargs)

    return wrapper
