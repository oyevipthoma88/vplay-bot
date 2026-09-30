import asyncio

# `bot/__init__.py` creates and installs the single event loop that every
# client must be bound to. It runs on this import, before anything below
# constructs a Pyrogram client — do not reorder these imports.
from bot import LOOP

from pyrogram import idle

from bot.core.clients import assistant, bot, call_py
from bot.logger import logger, setup_logging

# Importing these registers their @bot.on_message(...) handlers.
from bot.plugins import basic  # noqa: F401,E402
from bot.plugins import player  # noqa: F401,E402


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

        me = await bot.get_me()
        assistant_me = await assistant.get_me()
        logger.info(
            "VPlay started — bot @%s, assistant %s (id %s)",
            me.username,
            assistant_me.username or assistant_me.first_name,
            assistant_me.id,
        )

        await idle()
    finally:
        logger.info("Shutting down...")
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
