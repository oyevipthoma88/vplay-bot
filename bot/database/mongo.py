"""MongoDB storage via motor. If MONGO_DB_URI is not set (or fails), the
bot silently falls back to an in-memory store so it keeps working.
"""
import logging
from typing import List, Optional

from bot import config

logger = logging.getLogger(__name__)

_chats = set()
_users = set()
_log_channel: Optional[int] = config.LOG_CHANNEL_ID
_plays = 0
_db = None

if config.MONGO_DB_URI:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        _db = AsyncIOMotorClient(config.MONGO_DB_URI).get_database("vplay_bot")
        logger.info("MongoDB connected.")
    except Exception as e:  # noqa: BLE001
        logger.warning("MongoDB connect error: %s — in-memory store use hoga.", e)
        _db = None
else:
    logger.info("MONGO_DB_URI nahi diya — in-memory store use hoga.")


def is_connected() -> bool:
    return _db is not None


async def add_served_chat(chat_id: int, title: str = "") -> None:
    _chats.add(chat_id)
    if _db is not None:
        try:
            await _db.chats.update_one({"_id": chat_id}, {"$set": {"title": title}}, upsert=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo add_served_chat failed: %s", e)


async def remove_served_chat(chat_id: int) -> None:
    _chats.discard(chat_id)
    if _db is not None:
        try:
            await _db.chats.delete_one({"_id": chat_id})
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo remove_served_chat failed: %s", e)


async def get_served_chats() -> List[int]:
    if _db is not None:
        try:
            return [c["_id"] async for c in _db.chats.find({}, {"_id": 1})]
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo get_served_chats failed: %s", e)
    return list(_chats)


async def add_served_user(user_id: int, name: str = "") -> None:
    _users.add(user_id)
    if _db is not None:
        try:
            await _db.users.update_one({"_id": user_id}, {"$set": {"name": name}}, upsert=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo add_served_user failed: %s", e)


async def get_served_users() -> List[int]:
    if _db is not None:
        try:
            return [u["_id"] async for u in _db.users.find({}, {"_id": 1})]
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo get_served_users failed: %s", e)
    return list(_users)


async def get_setting(key: str):
    """Read a persistent setting (owner config). None if unavailable."""
    if _db is not None:
        try:
            doc = await _db.settings.find_one({"_id": f"owner_{key}"})
            return doc.get("value") if doc else None
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo get_setting(%s) failed: %s", key, e)
    return None


async def set_setting(key: str, value) -> None:
    """Write a persistent setting. Silently degrades to memory-only."""
    if _db is not None:
        try:
            await _db.settings.update_one(
                {"_id": f"owner_{key}"}, {"$set": {"value": value}}, upsert=True
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo set_setting(%s) failed: %s", key, e)


async def set_log_channel(channel_id: int) -> None:
    global _log_channel
    _log_channel = channel_id
    if _db is not None:
        try:
            await _db.settings.update_one(
                {"_id": "log_channel"}, {"$set": {"channel_id": channel_id}}, upsert=True
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo set_log_channel failed: %s", e)


async def get_log_channel() -> Optional[int]:
    if _db is not None:
        try:
            doc = await _db.settings.find_one({"_id": "log_channel"})
            if doc and doc.get("channel_id"):
                return doc["channel_id"]
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo get_log_channel failed: %s", e)
    return _log_channel


async def increment_play_count() -> None:
    global _plays
    _plays += 1
    if _db is not None:
        try:
            await _db.stats.update_one({"_id": "global"}, {"$inc": {"total_plays": 1}}, upsert=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo increment_play_count failed: %s", e)


async def get_stats() -> dict:
    plays = _plays
    if _db is not None:
        try:
            doc = await _db.stats.find_one({"_id": "global"})
            if doc:
                plays = doc.get("total_plays", plays)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo get_stats failed: %s", e)
    chats, users = len(_chats), len(_users)
    if _db is not None:
        try:
            chats = await _db.chats.count_documents({})
            users = await _db.users.count_documents({})
        except Exception as e:  # noqa: BLE001
            logger.warning("Mongo count failed: %s", e)
    return {
        "is_mongo": is_connected(),
        "total_chats": chats,
        "total_users": users,
        "total_plays": plays,
    }
