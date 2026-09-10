"""In-memory per-chat queue + now-playing state.

Kept in memory (not a database) on purpose — it's simple, it's fast, and a
restart naturally clears any stale playback state along with it. If the
dyno restarts, users just run /vplay again.
"""
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Track:
    source: str            # local file path OR a direct http(s) url
    title: str
    requested_by: str
    is_url: bool = False


@dataclass
class ChatState:
    queue: list = field(default_factory=list)
    current: Optional[Track] = None
    started_at: Optional[float] = None
    paused_at: Optional[float] = None
    paused_duration: float = 0.0
    is_paused: bool = False

    def elapsed(self) -> int:
        if self.started_at is None:
            return 0
        if self.is_paused and self.paused_at:
            return int(self.paused_at - self.started_at - self.paused_duration)
        return int(time.time() - self.started_at - self.paused_duration)


_states: dict = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in _states:
        _states[chat_id] = ChatState()
    return _states[chat_id]


def clear_state(chat_id: int) -> None:
    _states.pop(chat_id, None)
