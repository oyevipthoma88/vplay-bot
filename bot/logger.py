import logging
import sys

logger = logging.getLogger("vplay-bot")

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Libraries are chatty on INFO — keep them at WARNING so real errors still show.
    for noisy in ("pyrogram", "pytgcalls", "ntgcalls", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
