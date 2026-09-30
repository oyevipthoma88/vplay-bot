"""Shared command filter + de-duplication between the bot and the assistant
fallback listener.

The key is (chat, sender, date, text) instead of message id, because in
basic groups each account sees different message ids for the same message.
"""
import time

from pyrogram import filters

PREFIXES = ["/", "!"]


def cmd(commands):
    return filters.command(commands, prefixes=PREFIXES)


_seen: dict = {}
_TTL = 600


def _key(message):
    sender = 0
    if getattr(message, "from_user", None):
        sender = message.from_user.id
    elif getattr(message, "sender_chat", None):
        sender = message.sender_chat.id
    date = getattr(message, "date", None)
    ts = int(date.timestamp()) if date else 0
    text = (message.text or message.caption or "").strip()
    return (message.chat.id if message.chat else 0, sender, ts, text)


def _prune():
    now = time.time()
    for k in [k for k, v in _seen.items() if now - v > _TTL]:
        _seen.pop(k, None)


def mark_seen(message) -> bool:
    """Returns True if this is the first time we see the message."""
    _prune()
    key = _key(message)
    if key in _seen:
        return False
    _seen[key] = time.time()
    return True


def is_seen(message) -> bool:
    return _key(message) in _seen
