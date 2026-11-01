"""Storage helpers: temp cleanup + file-id persistence (crash-safe)."""
import asyncio
import json
import os
import time

from bot import config
from bot.logger import logger


def _downloads_dir() -> str:
    d = getattr(config, "DOWNLOADS_DIR", "/tmp/vplay_downloads")
    os.makedirs(d, exist_ok=True)
    return d


def _atomic_write(path: str, data) -> None:
    """Crash-safe JSON write (tmp + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cleanup_temp_files(max_age_hours: int = 24) -> int:
    """Delete old temp files. Returns count deleted."""
    d = _downloads_dir()
    if not os.path.isdir(d):
        return 0
    now = time.time()
    removed = 0
    for f in os.listdir(d):
        if f.endswith(".json") or f.endswith(".tmp"):
            continue
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        try:
            if (now - os.path.getmtime(p)) > max_age_hours * 3600:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("cleanup_temp_files: %s files deleted", removed)
    return removed


def _fileids_path(chat_id: int) -> str:
    return os.path.join(_downloads_dir(), f"fileids_{chat_id}.json")


def save_file_id(chat_id: int, track_title: str, file_id: str) -> None:
    path = _fileids_path(chat_id)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.loads(fh.read())
        except (ValueError, OSError):
            data = {}
    data[track_title[:80]] = file_id
    try:
        _atomic_write(path, data)
    except OSError as e:
        logger.warning("save_file_id failed: %s", e)


def get_file_id(chat_id: int, track_title: str):
    path = _fileids_path(chat_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (ValueError, OSError):
        return None
    return data.get(track_title[:80])


async def cleanup_loop(interval_seconds: int = 6 * 3600) -> None:
    """Background loop — run as asyncio task from __main__."""
    while True:
        try:
            cleanup_temp_files(max_age_hours=24)
        except Exception as e:  # noqa: BLE001
            logger.warning("cleanup_loop error: %s", e)
        await asyncio.sleep(interval_seconds)
