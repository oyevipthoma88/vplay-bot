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
import os

__version__ = "1.4.2"

# ---------------------------------------------------------------------------
# Dynamic library path fix for Heroku (apt buildpack)
# ---------------------------------------------------------------------------
# On Heroku, `heroku-buildpack-apt` installs ffmpeg and libpulse0 into /app/.apt.
# libpulsecommon-*.so lives in a `pulseaudio/` subfolder (e.g.
# /app/.apt/usr/lib/x86_64-linux-gnu/pulseaudio/). The apt buildpack's default
# LD_LIBRARY_PATH omits that subfolder, causing ffprobe/ffmpeg to fail with:
# "ffprobe: error while loading shared libraries: libpulsecommon-17.0.so"
# We scan /app/.apt for any pulseaudio/pulse library directories and prepend them
# to LD_LIBRARY_PATH so all child subprocesses (ffprobe, ffmpeg) find them cleanly.
def _setup_ffmpeg_ld_library_path() -> None:
    candidates = [
        "/app/.apt/usr/lib/x86_64-linux-gnu/pulseaudio",
        "/app/.apt/usr/lib/pulseaudio",
        "/app/.apt/lib/x86_64-linux-gnu/pulseaudio",
        "/app/.apt/lib/pulseaudio",
        "/usr/lib/x86_64-linux-gnu/pulseaudio",
        "/usr/lib/pulseaudio",
    ]
    apt_dir = "/app/.apt"
    if os.path.isdir(apt_dir):
        try:
            for root, dirs, files in os.walk(apt_dir):
                if "pulseaudio" in dirs:
                    candidates.append(os.path.join(root, "pulseaudio"))
                if any("libpulsecommon" in f for f in files):
                    if root not in candidates:
                        candidates.append(root)
        except Exception:
            pass

    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    existing = set(p for p in current_ld.split(":") if p)
    to_add = [d for d in candidates if os.path.isdir(d) and d not in existing]
    if to_add:
        os.environ["LD_LIBRARY_PATH"] = ":".join(to_add) + ((":" + current_ld) if current_ld else "")

_setup_ffmpeg_ld_library_path()

try:
    LOOP = asyncio.get_event_loop_policy().get_event_loop()
    if LOOP.is_closed():
        raise RuntimeError("closed loop")
except (RuntimeError, DeprecationWarning):
    LOOP = asyncio.new_event_loop()

asyncio.set_event_loop(LOOP)

__all__ = ["LOOP", "__version__"]
