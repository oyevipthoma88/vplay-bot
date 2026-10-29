import asyncio

# `bot/__init__.py` creates and installs the single event loop that every
# client must be bound to. It runs on this import, before anything below
# constructs a Pyrogram client — do not reorder these imports.
from bot import LOOP

from pyrogram import idle, raw
from pyrogram.types import BotCommand

from bot.core.clients import assistant, bot, call_py
from bot.logger import logger, setup_logging

# Importing these registers their @bot.on_message(...) handlers.
from bot.plugins import basic  # noqa: F401,E402
from bot.plugins import player  # noqa: F401,E402
from bot.plugins import diagnostics  # noqa: F401,E402
from bot.plugins import fallback  # noqa: F401,E402

from bot import __version__, config
from bot.core import runtime, streamer
from bot.core.logger_gc import send_log
from bot.core.player import warm_up_assistant_peers

_BOT_COMMANDS = [
    ("start", "Bot start karein"),
    ("vplay", "Video/audio par reply karke ya link ke saath play"),
    ("vpause", "Pause"),
    ("vresume", "Resume"),
    ("vskip", "Agla track"),
    ("vseek", "Seek — /vseek 90 ya 1:30"),
    ("vstop", "Stop aur video chat chhodo"),
    ("vloop", "Current track repeat ON/OFF"),
    ("vshuffle", "Queue shuffle"),
    ("vqueue", "Queue dekho"),
    ("vnowplaying", "Abhi kya chal raha hai"),
    ("vmute", "Mute"),
    ("vunmute", "Unmute"),
    ("stats", "Bot stats"),
    ("vdebug", "Bot/assistant status"),
    ("ping", "Bot alive check"),
    ("help", "Sab commands"),
]


async def _startup_diagnostics(me, assistant_me) -> None:
    handler_count = sum(len(g) for g in bot.dispatcher.groups.values())
    logger.info("Bot handlers registered: %s", handler_count)
    if handler_count == 0:
        logger.error("KOI HANDLER REGISTER NAHI HUA — commands ka jawab nahi aayega!")

    warnings = []
    try:
        users = await bot.invoke(raw.functions.users.GetUsers(id=[raw.types.InputUserSelf()]))
        raw_me = users[0]
        if getattr(raw_me, "bot_nochats", False):
            warnings.append(
                "BotFather me 'Allow Groups' OFF hai — bot group me add nahi ho sakta. "
                "@BotFather -> /setjoingroups -> Enable."
            )
        if not getattr(raw_me, "bot_chat_history", False):
            logger.info(
                "Bot privacy mode ON hai. Slash commands phir bhi aate hain; agar group "
                "me command ka jawab na aaye to @BotFather -> /setprivacy -> Disable."
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Bot settings check nahi ho paye: %s", e)

    dialogs = await warm_up_assistant_peers()
    logger.info("Assistant ke %s chats load hue", dialogs)

    try:
        await bot.set_bot_commands([BotCommand(c, d) for c, d in _BOT_COMMANDS])
    except Exception as e:  # noqa: BLE001
        logger.warning("Bot command menu set nahi ho paya: %s", e)

    for w in warnings:
        logger.warning(w)

    if config.STARTUP_NOTIFY:
        text = (
            f"✅ VPlay v{__version__} start ho gaya.\n"
            f"Bot: @{me.username}\nAssistant: {runtime.ASSISTANT_NAME} (id {assistant_me.id})\n"
            f"Handlers: {handler_count}"
        )
        if warnings:
            text += "\n\n⚠️ " + "\n⚠️ ".join(warnings)
        try:
            await bot.send_message(config.OWNER_ID, text)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Owner (%s) ko start message nahi bhej paya (%s). Owner ek baar bot ko "
                "private me /start bheje.", config.OWNER_ID, e,
            )


async def _safe_stop(name: str, coro_factory) -> None:
    try:
        await coro_factory()
    except Exception as e:  # noqa: BLE001 - shutdown must never mask the real error
        logger.warning("Error while stopping %s: %s", name, e)


async def main():
    setup_logging()

    started_bot = started_assistant = started_calls = False
    try:
        await bot.start()
        started_bot = True

        await assistant.start()
        started_assistant = True

        await call_py.start()
        started_calls = True

        await streamer.start_streamer()

        me = await bot.get_me()
        assistant_me = await assistant.get_me()
        logger.info(
            "VPlay started — bot @%s, assistant %s (id %s)",
            me.username,
            assistant_me.username or assistant_me.first_name,
            assistant_me.id,
        )
        runtime.BOT_USERNAME = me.username or ""
        runtime.BOT_ID = me.id
        runtime.ASSISTANT_ID = assistant_me.id
        runtime.ASSISTANT_NAME = (
            f"@{assistant_me.username}" if assistant_me.username else assistant_me.first_name
        )
        try:
            await _startup_diagnostics(me, assistant_me)
        except Exception:  # noqa: BLE001 - diagnostics must never stop the bot
            logger.exception("Startup diagnostics failed")
        try:
            await send_log(f"🚀 <b>VPlay v{__version__} start</b>\nBot: @{me.username}\n⚡ 0s streamer ON")
        except Exception:  # noqa: BLE001
            pass
        logger.info("Ready — ab group me /vplay bhejein. Har command yahan log hoga.")

        await idle()
    finally:
        logger.info("Shutting down...")
        await _safe_stop("streamer", streamer.stop_streamer)
        if started_calls:
            await _safe_stop("pytgcalls", lambda: call_py.stop())
        if started_assistant:
            await _safe_stop("assistant", lambda: assistant.stop())
        if started_bot:
            await _safe_stop("bot", lambda: bot.stop())


def _run() -> None:
    try:
        LOOP.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(LOOP) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                LOOP.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            LOOP.run_until_complete(LOOP.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        finally:
            if not LOOP.is_closed():
                LOOP.close()


if __name__ == "__main__":
    _run()
