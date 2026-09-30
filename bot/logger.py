import logging

logger = logging.getLogger("vplay-bot")

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # Pyrogram/Kurigram and PyTgCalls are quite chatty on INFO — keep them quieter.
    for noisy in ("pyrogram", "pytgcalls", "ntgcalls", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
