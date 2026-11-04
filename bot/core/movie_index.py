"""Local index of every playable file in the movie channel(s).

ROOT FIX for "/vplay toxic — channel me movie hai phir bhi 'nahi mili'":

* Telegram's server search matches captions far better than file names and
  rate-limits heavy use (FloodWait -> empty result -> "nahi mili"). The old
  code also re-scanned up to 1500 messages of EVERY channel on EVERY
  /vplay, which was slow and blocked the event loop (that also starved the
  running stream — another reason for the mid-play quality drop).
* Now the assistant reads each movie channel ONCE in the background and
  keeps a small in-memory index (file name + caption + size + date).
  /vplay searches that index locally: instant, typo-tolerant, no FloodWait.
* New uploads are added the moment they are posted (bot + assistant both
  listen to channel posts), edits update the entry, deletions remove it,
  and a cheap incremental refresh runs every few minutes as a safety net.
* Results are sorted LATEST UPLOAD FIRST, so the newest file is always on
  top of the button list.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from pyrogram import enums, filters
from pyrogram.errors import FloodWait

from bot import config
from bot.core.clients import assistant, bot
from bot.logger import logger

try:
    from rapidfuzz import fuzz as _fuzz
    from rapidfuzz import process as _process
    _HAS_FUZZ = True
except ImportError:  # pragma: no cover
    _HAS_FUZZ = False

_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp")
_AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav")
_FILTERS = (
    enums.MessagesFilter.VIDEO,
    enums.MessagesFilter.DOCUMENT,
    enums.MessagesFilter.AUDIO,
)
_STOP = {
    "movie", "movies", "film", "full", "hd", "the", "a", "an", "new", "latest",
    "hindi", "dubbed", "download", "watch", "online", "video", "part", "ka", "ki", "ke",
}
# Words that end the real title inside a release file name.
_JUNK = re.compile(
    r"\b(19\d{2}|20\d{2}|480p|540p|720p|1080p|2160p|4k|uhd|fhd|hdrip|web ?dl|webrip|web|"
    r"bluray|blu ray|brrip|bdrip|dvdrip|dvdscr|hdcam|camrip|hdts|hdtc|pre ?dvd|x264|x265|"
    r"h264|h 264|h265|h 265|hevc|10bit|8bit|aac|ddp?\d?|dd5|atmos|esub|esubs|msubs|"
    r"dual audio|multi audio|hq|proper|uncut|extended|org|line audio|s\d{1,2}e\d{1,3}|"
    r"s\d{1,2}|season \d+|ep ?\d+|episode \d+|complete)\b",
    re.I,
)
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_QUAL = re.compile(r"(?<!\d)(2160p|1080p|720p|480p|360p|4k)(?!\w)", re.I)
_EPISODE = re.compile(r"\bs(\d{1,2})\s?e(\d{1,3})\b", re.I)
_GENERIC = re.compile(r"^(?:(?:file|video|movie|vid|doc|document|media|clip|new|upload|dl|\d+)\s*)*$", re.I)
_MENTION = re.compile(r"(@\w+|https?://\S+|t\.me/\S+|www\.\S+)", re.I)


def normalize(text: str) -> str:
    t = (text or "").lower().replace("_", " ")
    t = _MENTION.sub(" ", t)
    t = re.sub(r"[\.\-_\[\]\(\)\{\}\+,/\\|~`!#$%^&*=:;\"'<>?]+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)  # emojis etc.
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Entry:
    chat_id: int
    msg_id: int
    uid: str
    title: str
    year: str
    quality: str
    episode: str
    size: int
    duration: int
    date: int
    is_video: bool
    norm: str
    compact: str
    tokens: Set[str] = field(default_factory=set)

    @property
    def key(self) -> Tuple[int, int]:
        return (self.chat_id, self.msg_id)


_entries: Dict[Tuple[int, int], Entry] = {}
_vocab: Dict[str, Set[Tuple[int, int]]] = {}
_channels: Dict[int, dict] = {}   # chat_id -> {"title", "max_id"}
_state = {"ready": False, "building": False, "last_full": 0.0, "last_refresh": 0.0, "error": ""}
_build_lock: Optional[asyncio.Lock] = None


def _lock() -> asyncio.Lock:
    global _build_lock
    if _build_lock is None:
        _build_lock = asyncio.Lock()
    return _build_lock


# ------------------------------------------------------------------ parse
def media_of(msg):
    for attr in ("video", "document", "audio", "animation"):
        media = getattr(msg, attr, None)
        if media is not None:
            return attr, media
    return None, None


def is_playable(msg) -> bool:
    kind, media = media_of(msg)
    if kind is None:
        return False
    if kind == "animation":
        return (getattr(media, "duration", 0) or 0) > 60  # skip GIF-like clips
    if kind != "document":
        return True
    mime = (getattr(media, "mime_type", "") or "").lower()
    name = (getattr(media, "file_name", "") or "").lower()
    if mime.startswith(("video", "audio")) or mime in ("application/x-matroska", "application/x-mpegurl"):
        return True
    return name.endswith(_VIDEO_EXT + _AUDIO_EXT)


def clean_title(raw: str) -> Tuple[str, str, str, str]:
    """Release name -> (title, year, quality, episode)."""
    first = (raw or "").strip().split("\n")[0]
    first = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v|ts|flv|wmv|mp3|m4a|aac|flac)$", "", first, flags=re.I)
    spaced = normalize(first)
    ym = _YEAR.search(spaced)
    year = ym.group(1) if ym else ""
    qm = _QUAL.search(spaced)
    quality = qm.group(1).lower() if qm else ""
    if quality == "4k":
        quality = "2160p"
    em = _EPISODE.search(spaced)
    episode = f"S{int(em.group(1)):02d}E{int(em.group(2)):02d}" if em else ""
    m = _JUNK.search(spaced)
    title = spaced[: m.start()].strip() if m and m.start() > 1 else spaced
    title = " ".join(w.capitalize() if not w.isupper() else w for w in title.split()[:10])
    return title or "Movie", year, quality, episode


def entry_from(msg) -> Optional[Entry]:
    if msg is None or getattr(msg, "empty", False) or not is_playable(msg):
        return None
    kind, media = media_of(msg)
    chat = getattr(msg, "chat", None)
    if chat is None:
        return None
    name = getattr(media, "file_name", None) or ""
    tag = " ".join(x for x in (getattr(media, "title", None), getattr(media, "performer", None)) if x)
    cap = (getattr(msg, "caption", None) or "").strip()
    cap_first = cap.split("\n")[0][:200]
    display_src = name or tag or cap_first or "Movie"
    # Caption often has the clean name while the file is "file_123.mkv" or
    # "@channel_1080p.mkv" — use the caption when the file name is generic.
    if cap_first and (not name or _GENERIC.match(normalize(clean_title(name)[0]) or "")):
        display_src = cap_first
    title, year, quality, episode = clean_title(display_src)
    if not quality:
        qm = _QUAL.search(normalize(f"{name} {cap[:300]}"))
        quality = qm.group(1).lower() if qm else ""
    if not year:
        ym = _YEAR.search(normalize(f"{name} {cap[:300]}"))
        year = ym.group(1) if ym else ""
    norm = normalize(f"{name} {tag} {cap[:400]}")
    mime = (getattr(media, "mime_type", "") or "").lower()
    is_video = kind in ("video", "animation") or mime.startswith("video") or name.lower().endswith(_VIDEO_EXT)
    date = getattr(msg, "date", None)
    return Entry(
        chat_id=chat.id,
        msg_id=msg.id,
        uid=getattr(media, "file_unique_id", "") or f"{chat.id}:{msg.id}",
        title=title,
        year=year,
        quality=quality,
        episode=episode,
        size=int(getattr(media, "file_size", 0) or 0),
        duration=int(getattr(media, "duration", 0) or 0),
        date=int(date.timestamp()) if date else 0,
        is_video=is_video,
        norm=norm,
        compact=norm.replace(" ", ""),
        tokens=set(norm.split()),
    )


# ------------------------------------------------------------- mutations
def _remove(key) -> None:
    old = _entries.pop(key, None)
    if old is None:
        return
    for t in old.tokens:
        s = _vocab.get(t)
        if s is not None:
            s.discard(key)
            if not s:
                _vocab.pop(t, None)


def add_message(msg) -> bool:
    e = entry_from(msg)
    if e is None:
        return False
    _remove(e.key)
    _entries[e.key] = e
    for t in e.tokens:
        _vocab.setdefault(t, set()).add(e.key)
    ch = _channels.setdefault(e.chat_id, {"title": getattr(msg.chat, "title", "") or "", "max_id": 0})
    ch["max_id"] = max(ch.get("max_id", 0), e.msg_id)
    return True


def forget(chat_id: int, msg_id: int) -> None:
    _remove((chat_id, msg_id))


# ---------------------------------------------------------------- channels
async def _resolve_channels() -> List[int]:
    from bot.core.movie_search import movie_channels

    ids: List[int] = []
    for ref in movie_channels():
        try:
            chat = await assistant.get_chat(ref)
            if chat.id not in ids:
                ids.append(chat.id)
                _channels.setdefault(chat.id, {"title": chat.title or "", "max_id": 0})
        except Exception as e:  # noqa: BLE001
            logger.warning("movie_index: configured channel %r resolve fail: %s", ref, e)

    admin_ch, other_ch = [], []
    try:
        async for d in assistant.get_dialogs():
            c = getattr(d, "chat", None)
            if c is None or c.type != enums.ChatType.CHANNEL:
                continue
            (admin_ch if (c.is_admin or c.is_creator) else other_ch).append(c)
    except Exception as e:  # noqa: BLE001
        logger.warning("movie_index: dialogs fail: %s", e)

    for c in admin_ch:
        if c.id not in ids:
            ids.append(c.id)
            _channels.setdefault(c.id, {"title": c.title or "", "max_id": 0})
    # Channels the bot already posted-indexed (bot is admin there).
    for cid in list(_channels):
        if cid not in ids:
            ids.append(cid)
    if not ids:
        # Last resort: channels the assistant has only joined.
        for c in other_ch[: max(1, int(getattr(config, "MOVIE_AUTO_MAX", 10) or 10))]:
            ids.append(c.id)
            _channels.setdefault(c.id, {"title": c.title or "", "max_id": 0})
    return ids


async def _index_channel(chat_id: int, min_id: int = 0):
    """Returns (added, seen_keys, complete)."""
    limit = max(0, int(getattr(config, "MOVIE_INDEX_LIMIT", 20000) or 0))
    added, seen_keys, complete = 0, set(), True
    for flt in _FILTERS:
        offset_id, tries, seen = 0, 0, 0
        while True:
            try:
                async for msg in assistant.search_messages(
                    chat_id, filter=flt, min_id=min_id, offset_id=offset_id,
                    limit=max(0, limit - seen) if limit else 0,
                ):
                    offset_id = msg.id
                    seen += 1
                    if add_message(msg):
                        added += 1
                        seen_keys.add((chat_id, msg.id))
                    if seen % 200 == 0:
                        await asyncio.sleep(0.2)  # be gentle — a stream may be running
                break
            except FloodWait as e:
                tries += 1
                if tries > 5:
                    complete = False
                    break
                logger.info("movie_index FloodWait %ss (chat %s)", e.value, chat_id)
                await asyncio.sleep(e.value + 1)
            except Exception as e:  # noqa: BLE001
                logger.warning("movie_index: chat %s filter %s fail: %s", chat_id, flt.name, e)
                complete = False
                break
        if limit and seen >= limit:
            complete = False  # capped — keep older entries we may still have
    return added, seen_keys, complete


async def rebuild(full: bool = True) -> dict:
    """Full (or incremental) index of all movie channels."""
    async with _lock():
        _state["building"] = True
        t0 = time.time()
        try:
            chans = await _resolve_channels() if (full or not _channels) else list(_channels)
            total_new = 0
            for cid in chans:
                min_id = 0 if full else int(_channels.get(cid, {}).get("max_id", 0) or 0)
                before = {k for k in _entries if k[0] == cid}
                added, seen_keys, complete = await _index_channel(cid, min_id=min_id)
                total_new += len(seen_keys - before)
                if full and complete:
                    for k in before - seen_keys:  # deleted from the channel
                        _remove(k)
            _state["ready"] = True
            _state["error"] = ""
            _state["last_refresh"] = time.time()
            if full:
                _state["last_full"] = time.time()
            logger.info(
                "movie_index %s: %d channel(s), %d files (+%d) in %.1fs",
                "full" if full else "refresh", len(chans), len(_entries), total_new, time.time() - t0,
            )
            return {"channels": len(chans), "files": len(_entries), "new": total_new}
        except Exception as e:  # noqa: BLE001
            _state["error"] = str(e)[:200]
            logger.exception("movie_index rebuild fail")
            raise
        finally:
            _state["building"] = False


async def run_forever() -> None:
    """Background task started from __main__."""
    if not getattr(config, "MOVIE_SEARCH_ENABLED", True):
        return
    await asyncio.sleep(3)
    refresh_every = max(120, int(getattr(config, "MOVIE_INDEX_REFRESH", 600) or 600))
    full_every = 6 * 60 * 60
    while True:
        try:
            full = not _state["ready"] or time.time() - _state["last_full"] > full_every
            await rebuild(full=full)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(refresh_every if _state["ready"] else 60)


def status() -> dict:
    return {
        "ready": _state["ready"],
        "building": _state["building"],
        "files": len(_entries),
        "channels": [(cid, c.get("title", "")) for cid, c in _channels.items()],
        "last_refresh": _state["last_refresh"],
        "error": _state["error"],
    }


def is_ready() -> bool:
    return bool(_state["ready"] and _entries)


# ------------------------------------------------------------------ search
def _correct(q: str) -> Optional[str]:
    try:
        from bot.core.movie_search import COMMON_FIXES
    except Exception:  # noqa: BLE001
        return None
    return COMMON_FIXES.get(q)


def _search_sync(entries: List[Entry], vocab: Dict[str, tuple], query: str, limit: int):
    qn = normalize(query)
    if not qn:
        return [], False
    variants = [qn]
    fx = _correct(qn)
    if fx and normalize(fx) not in variants:
        variants.append(normalize(fx))
    by_key = {e.key: e for e in entries}
    found: Dict[tuple, Tuple[int, float]] = {}  # key -> (tier, score)

    # Tier 3: whole phrase (spaces ignored: "hanumanansh" == "hanuman ansh").
    for v in variants:
        vc = v.replace(" ", "")
        for e in entries:
            if v in e.norm or (len(vc) >= 4 and vc in e.compact):
                found[e.key] = (3, 100.0)

    # Tier 2 / 1: every meaningful word matches (exact, prefix or typo).
    words = list(vocab)
    for v in variants:
        toks = [t for t in v.split() if len(t) >= 3 and t not in _STOP] or [t for t in v.split() if len(t) >= 2]
        if not toks:
            continue
        per_tok: List[Dict[tuple, float]] = []
        for t in toks:
            hits: Dict[str, float] = {}
            if t in vocab:
                hits[t] = 100.0
            if len(t) >= 3:
                for w in words:
                    if w != t and (w.startswith(t) or (len(t) >= 4 and t in w)):
                        hits.setdefault(w, 96.0)
            if _HAS_FUZZ and len(t) >= 5:
                cut = 80
                for w, sc, _ in _process.extract(t, words, scorer=_fuzz.ratio, score_cutoff=cut, limit=40):
                    hits.setdefault(w, float(sc))
            keys: Dict[tuple, float] = {}
            for w, sc in hits.items():
                for k in vocab.get(w, ()):
                    if sc > keys.get(k, 0):
                        keys[k] = sc
            per_tok.append(keys)
        common = set(per_tok[0])
        for d in per_tok[1:]:
            common &= set(d)
        for k in common:
            sc = sum(d[k] for d in per_tok) / len(per_tok)
            tier = 2 if sc >= 95 else 1
            if k not in found or found[k][0] < tier:
                found[k] = (tier, sc)

    suggested = False
    if not found and _HAS_FUZZ and entries:
        # "Did you mean": closest titles overall.
        choices = [e.norm[:120] for e in entries]
        for _, sc, i in _process.extract(qn, choices, scorer=_fuzz.WRatio, score_cutoff=72, limit=limit):
            found[entries[i].key] = (0, float(sc))
        suggested = bool(found)

    strong = [(by_key[k], s) for k, (t, s) in found.items() if t >= 2]
    weak = [(by_key[k], s) for k, (t, s) in found.items() if t < 2]
    strong.sort(key=lambda x: (x[0].date, x[0].msg_id), reverse=True)        # latest first
    weak.sort(key=lambda x: (round(x[1] / 5), x[0].date), reverse=True)
    out, seen_uid = [], set()
    for e, _ in strong + weak:
        if e.uid in seen_uid:
            continue
        seen_uid.add(e.uid)
        out.append(e)
        if len(out) >= limit:
            break
    return out, suggested


async def search(query: str, limit: int = 30) -> Tuple[List[Entry], bool]:
    """Returns (entries latest-first, suggested_only). Runs off the event loop."""
    entries = list(_entries.values())
    if not entries:
        return [], False
    vocab = {w: tuple(s) for w, s in _vocab.items()}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _search_sync, entries, vocab, query, limit)


# --------------------------------------------------------- live updates
_media_filter = filters.channel & (filters.video | filters.document | filters.audio | filters.animation)


@bot.on_message(_media_filter, group=7)
async def _bot_new_post(_client, message):
    # The bot only gets posts from channels where it is ADMIN — i.e. the
    # movie channel. Always index those.
    if add_message(message):
        logger.info("movie_index: new upload (bot) chat=%s id=%s", message.chat.id, message.id)


@assistant.on_message(_media_filter, group=7)
async def _assistant_new_post(_client, message):
    if message.chat and message.chat.id in _channels and add_message(message):
        logger.info("movie_index: new upload chat=%s id=%s", message.chat.id, message.id)


@bot.on_edited_message(_media_filter, group=7)
async def _bot_edit(_client, message):
    add_message(message)


@assistant.on_edited_message(_media_filter, group=7)
async def _assistant_edit(_client, message):
    if message.chat and message.chat.id in _channels:
        add_message(message)


async def _on_deleted(_client, messages):
    for m in messages or []:
        chat = getattr(m, "chat", None)
        if chat is not None:
            forget(chat.id, m.id)


try:
    bot.on_deleted_messages(group=7)(_on_deleted)
    assistant.on_deleted_messages(group=7)(_on_deleted)
except Exception as _e:  # noqa: BLE001
    logger.debug("deleted-messages handler skip: %s", _e)
