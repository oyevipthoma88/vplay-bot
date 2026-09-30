"""Two Telegram clients are used, which is the standard way these
group-video-chat bots work:

- `bot`: a normal Bot API account (BOT_TOKEN). Handles commands.
- `assistant`: a *user* account (SESSION_STRING) that PyTgCalls uses to
  actually join and stream into the group's video/voice chat — regular bot
  accounts are not able to join group calls themselves.

The assistant account must be a member (ideally admin, with "Manage Video
Chats") of any group you want to play media in.

Both clients are constructed here at import time because the plugins need
them for their decorators. `bot/__init__.py` has already installed the one
shared event loop by the time this module runs — see the long note there.
"""
import os

from pyrogram import Client
from pytgcalls import PyTgCalls

from bot import LOOP  # noqa: F401 - imported for its loop-installing side effect
from bot import config

os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)

bot = Client(
    name="vplay-bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    in_memory=True,
)

assistant = Client(
    name="vplay-assistant",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    session_string=config.SESSION_STRING,
    in_memory=True,
)

# Belt and braces: if any library version resolves its loop lazily or
# differently, pin both clients to the shared loop explicitly.
for _client in (bot, assistant):
    try:
        _client.loop = LOOP
    except Exception:  # noqa: BLE001 - attribute may be read-only in some versions
        pass

call_py = PyTgCalls(assistant)

try:
    call_py.loop = LOOP
except Exception:  # noqa: BLE001
    pass
