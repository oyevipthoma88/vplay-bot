"""Run: python -m pytest tests/  (no Telegram connection needed)."""
import asyncio
import datetime as dt
import os
from types import SimpleNamespace as N

for k, v in {"API_ID": "1", "API_HASH": "x", "BOT_TOKEN": "1:x", "SESSION_STRING": "x", "OWNER_ID": "1"}.items():
    os.environ.setdefault(k, v)

from bot.core import movie_index as mi  # noqa: E402

CHAT = N(id=-1001, title="Movies")


def _msg(i, name, cap="", days=0, kind="document", mime="video/x-matroska", uid=None):
    media = N(file_name=name, mime_type=mime, file_size=2 * 1024 ** 3, duration=7800,
              file_unique_id=uid or f"u{i}", title=None, performer=None)
    m = N(id=i, chat=CHAT, caption=cap, date=dt.datetime.now() - dt.timedelta(days=days),
          empty=False, video=None, document=None, audio=None, animation=None)
    setattr(m, kind, media)
    return m


def _load():
    mi._entries.clear(); mi._vocab.clear(); mi._channels.clear()
    for m in [
        _msg(1, "Toxic.2026.720p.HDRip.Hindi.mkv", days=20),
        _msg(2, "@MoviesHub_TOXIC_2026_1080p_WEB-DL.mkv", days=1),
        _msg(3, "file_123.mkv", "🍿 Toxic A Fairy Tale (2026) 480p", days=0),
        _msg(4, "Hanuman.Ansh.2025.1080p.WEB-DL.HEVC.mkv", days=3),
        _msg(5, "HanumanAnsh_2025_720p.mp4", days=0, kind="video", mime="video/mp4"),
        _msg(6, "Mirzapur.S03E01.1080p.mkv", days=10),
        _msg(7, "Jailer.2023.Tamil.720p.mkv", mime="application/octet-stream"),
        _msg(8, "notes.pdf", mime="application/pdf"),
        _msg(9, "Toxic.2026.720p.HDRip.Hindi.mkv", days=30, uid="u1"),
    ]:
        mi.add_message(m)
    mi._state["ready"] = True


def _ids(q):
    res, sugg = asyncio.run(mi.search(q))
    return [e.msg_id for e in res], sugg


def test_toxic_finds_all_uploads_latest_first():
    _load()
    assert _ids("toxic") == ([3, 2, 1], False)


def test_space_insensitive_and_latest_first():
    _load()
    assert _ids("hanuman ansh") == ([5, 4], False)
    assert _ids("hanumanansh") == ([5, 4], False)


def test_typos():
    _load()
    assert _ids("mirjapur")[0] == [6]
    assert _ids("jailr")[0] == [7]


def test_non_video_document_ignored_and_duplicates_dropped():
    _load()
    assert (-1001, 8) not in mi._entries
    assert 9 not in _ids("toxic")[0]


def test_unknown_title_returns_nothing():
    _load()
    assert _ids("pushpa") == ([], False)


def test_caption_used_for_generic_file_name():
    _load()
    assert mi._entries[(-1001, 3)].title == "Toxic A Fairy Tale"
