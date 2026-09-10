import logging


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # Pyrogram/Kurigram are quite chatty on INFO — keep them quieter.
    logging.getLogger("pyrogram").setLevel(logging.WARNING)


logger = logging.getLogger("vplay-bot")
