"""Owner panel state — Mongo-backed with in-memory fallback.

Heroku restart pe start pic / default thumb / maintenance mode gayab nahi hote.
`hydrate()` startup pe ek baar chalna chahiye (__main__.py me wired hai).
"""
import asyncio

from bot.logger import logger

_state = {
    "maintenance": False,
    "start_pic": None,
    "default_thumb": None,
    "movie_channel": None,
}
_hydrated = False


def _schedule(key: str, value) -> None:
    """Fire-and-forget persist; never blocks the caller."""
    try:
        from bot.database.mongo import set_setting
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(set_setting(key, value))
    except Exception as e:  # noqa: BLE001
        logger.info("owner_data persist(%s) skipped: %s", key, e)


async def hydrate() -> None:
    """Load persisted owner state from Mongo (once per process)."""
    global _hydrated
    if _hydrated:
        return
    _hydrated = True
    try:
        from bot.database.mongo import get_setting
    except ImportError:
        return
    for key in ("maintenance", "start_pic", "default_thumb", "movie_channel", "movie_channels"):
        try:
            val = await get_setting(key)
        except Exception as e:  # noqa: BLE001
            logger.warning("owner_data hydrate(%s) failed: %s", key, e)
            continue
        if val is None:
            continue
        if key == "movie_channels":
            _state[key] = list(val) if isinstance(val, (list, tuple)) else []
            continue
        _state[key] = bool(val) if key == "maintenance" else val
    logger.info(
        "owner_data hydrated — maintenance=%s start_pic=%s thumb=%s",
        _state["maintenance"],
        bool(_state["start_pic"]),
        bool(_state["default_thumb"]),
    )


def is_maintenance() -> bool:
    return bool(_state["maintenance"])


def set_maintenance(on: bool) -> None:
    _state["maintenance"] = bool(on)
    _schedule("maintenance", bool(on))


def get_start_pic():
    return _state["start_pic"]


def set_start_pic(file_id) -> None:
    _state["start_pic"] = file_id
    _schedule("start_pic", file_id)


def get_default_thumb():
    return _state["default_thumb"]


def set_default_thumb(file_id) -> None:
    _state["default_thumb"] = file_id
    _schedule("default_thumb", file_id)


def get_movie_channel():
    """Backward-compat: primary (first) channel."""
    chs = _state.get("movie_channels") or []
    if chs:
        return chs[0]
    return _state["movie_channel"]


def get_movie_channels():
    """List of configured movie channels (newest order preserved)."""
    chs = _state.get("movie_channels") or []
    if chs:
        return list(chs)
    primary = _state["movie_channel"]
    return [primary] if primary else []


def add_movie_channel(chat_ref) -> list:
    chs = _state.get("movie_channels") or []
    if _state["movie_channel"] and _state["movie_channel"] not in chs:
        chs.append(_state["movie_channel"])
    if chat_ref not in chs:
        chs.append(chat_ref)
    _state["movie_channels"] = chs
    _state["movie_channel"] = chs[0] if chs else None
    _schedule("movie_channels", chs)
    _schedule("movie_channel", _state["movie_channel"])
    return chs


def remove_movie_channel(chat_ref) -> list:
    chs = [c for c in (_state.get("movie_channels") or []) if c != chat_ref]
    if _state["movie_channel"] == chat_ref:
        _state["movie_channel"] = chs[0] if chs else None
    _state["movie_channels"] = chs
    _schedule("movie_channels", chs)
    _schedule("movie_channel", _state["movie_channel"])
    return chs


def set_movie_channel(chat_ref) -> None:
    """Set primary channel (also seeds the list)."""
    _state["movie_channel"] = chat_ref
    if chat_ref and chat_ref not in (_state.get("movie_channels") or []):
        chs = _state.get("movie_channels") or []
        chs.insert(0, chat_ref)
        _state["movie_channels"] = chs
        _schedule("movie_channels", chs)
    _schedule("movie_channel", chat_ref)
