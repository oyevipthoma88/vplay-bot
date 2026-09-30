"""VPlay — Telegram group video/voice-chat player bot.

IMPORTANT — event loop setup.

Pyrogram/Kurigram `Client` objects (and `PyTgCalls`) capture
`asyncio.get_event_loop()` at *construction* time. Because the clients in
`bot.core.clients` are created at import time (the plugins need them for
their `@bot.on_message(...)` decorators), they bind to whatever loop is
current during import.

`asyncio.run()` then creates a *brand new* loop and runs on that, so every
internal future the client created belongs to the old loop. That is exactly
the crash that was filling the logs:

    RuntimeError: Task ... got Future ... attached to a different loop

The fix is to create ONE loop here — this module is guaranteed to be
imported before any `bot.*` submodule — install it as the current loop, and
then run the app on that same loop (see `bot/__main__.py`).
"""
import asyncio

__version__ = "1.1.0"

try:
    LOOP = asyncio.get_event_loop_policy().get_event_loop()
    if LOOP.is_closed():
        raise RuntimeError("closed loop")
except (RuntimeError, DeprecationWarning):
    LOOP = asyncio.new_event_loop()

asyncio.set_event_loop(LOOP)

__all__ = ["LOOP", "__version__"]
