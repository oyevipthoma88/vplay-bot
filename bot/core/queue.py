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
    duration: int = 0      # seconds, 0 = unknown (links / live)
    mode: str = "stream"   # "stream" (Telegram 0s stream) | "link" | "file"
    is_video: bool = True
    requested_by_id: int = 0


@dataclass
class ChatState:
    queue: list = field(default_factory=list)
    current: Optional[Track] = None
    started_at: Optional[float] = None
    paused_at: Optional[float] = None
    paused_duration: float = 0.0
    is_paused: bool = False
    is_muted: bool = False
    loop_mode: str = "off"  # "off" | "track" | "queue"
    volume: int = 100  # 1-200
    # Bumped every time a new stream starts — lets the stream-end handler
    # ignore duplicate/late "ended" events for an older track.
    generation: int = 0
    # Wall-clock time the current stream (re)started — used to ignore the
    # duplicate/late "stream ended" events PyTgCalls sends around a switch.
    last_start: float = 0.0

    def elapsed(self) -> int:
        if self.started_at is None:
            return 0
        if self.is_paused and self.paused_at:
            value = self.paused_at - self.started_at - self.paused_duration
        else:
            value = time.time() - self.started_at - self.paused_duration
        value = max(0, int(value))
        if self.current and self.current.duration:
            value = min(value, self.current.duration)
        return value


_states: dict = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in _states:
        _states[chat_id] = ChatState()
    return _states[chat_id]


def clear_state(chat_id: int) -> None:
    _states.pop(chat_id, None)


def active_chats() -> int:
    return sum(1 for s in _states.values() if s.current is not None)
