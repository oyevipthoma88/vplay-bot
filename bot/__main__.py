import asyncio

from pyrogram import idle

from bot.core.clients import assistant, bot, call_py
from bot.logger import logger, setup_logging

# Importing these registers their @bot.on_message(...) handlers.
from bot.plugins import basic  # noqa: F401
from bot.plugins import player  # noqa: F401


async def main():
    setup_logging()

    await bot.start()
    await assistant.start()
    await call_py.start()

    me = await bot.get_me()
    logger.info("VPlay bot started as @%s", me.username)

    await idle()

    logger.info("Shutting down...")
    await call_py.stop()
    await assistant.stop()
    await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
