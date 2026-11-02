"""Movie search engine — multi-channel, fuzzy, parallel.

Architecture (adapted from Rohan_movies reference):
  1. MULTI-CHANNEL   — comma-separated list, searched in parallel (asyncio.gather)
  2. FUZZY MATCH     — rapidfuzz WRatio (typos work: "mirjapur" -> "mirzapur")
  3. PARSE_CAPTION   — extracts title/year/quality from filename/caption
  4. SPELLING FIX    — COMMON_FIXES map (Indian movies) + fuzzy fallback
  5. FALLBACK        — all channels -> global assistant chats

Runs on the ASSISTANT account (bot account cannot read channel history).
Results cached per normalized query.
"""
import asyncio
import html
import re
import time
from typing import List, Optional, Tuple

from pyrogram import enums
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from bot import config
from bot.core import streamer
from bot.core.clients import assistant
from bot.core.queue import Track
from bot.core.streamer import get_stream_url
from bot.logger import logger

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _HAS_FUZZ = True
except ImportError:
    _HAS_FUZZ = False
    logger.warning("rapidfuzz missing — token scoring fallback (pip install rapidfuzz)")

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_MEDIA_ATTRS = ("video", "document", "audio", "animation", "voice", "video_note")
_FILTERS = (
    enums.MessagesFilter.VIDEO,
    enums.MessagesFilter.DOCUMENT,
    enums.MessagesFilter.AUDIO,
)

# Indian movie spelling corrections (from Rohan_movies spelling.py)
COMMON_FIXES = {
    "mirjapur": "mirzapur", "mirjapurr": "mirzapur", "mirzapurr": "mirzapur",
    "hanumanash": "hanumanansh", "hanuman ansh": "hanumanansh",
    "suryavanshi": "sooryavanshi", "suryavansh": "sooryavanshi",
    "brahmashtra": "brahmastra", "brahmaastra": "brahmastra",
    "krishh": "krrish", "krissh": "krrish", "krisshh": "krrish",
    "rajneeti": "raajneeti",
    "agneepaath": "agneepath",
    "sholayy": "sholay", "sholaay": "sholay",
    "donnd": "don",
}

_CACHE: dict = {}
_CACHE_TTL = 240
_MISS_TTL = 60
_LAST_CALL: dict = {}
_MIN_INTERVAL = 0.8
_MIN_SCORE = 60

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_QUALITY_RE = re.compile(
    r"\b(480p|720p|1080p|2160p|4k|hdrip|web-?dl|webrip|bluray|hdcam|hdtc|hdts|dvdrip)\b",
    re.IGNORECASE,
)


# ------------------------------------------------------------ helpers
def normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[\.\-_\[\]\(\)\{\}\+,/\\|~`!@#$%^&*+=:;\"'<>?]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _tokens(query: str):
    return [w for w in normalize(query).split() if len(w) >= 2]


def parse_caption(text: str) -> dict:
    """Extract title/year/quality from caption or filename."""
    raw = (text or "").strip()
    first = raw.split("\n")[0].strip()
    base = first.rsplit(".", 1)[0] if "." in first.rsplit("/", 1)[-1] else first
    base = re.sub(r"\.(mkv|mp4|avi|mov|webm|m4v|ts)$", "", base, flags=re.I)

    year_m = _YEAR_RE.search(base)
    year = year_m.group(0) if year_m else ""
    quality_m = _QUALITY_RE.search(base)
    quality = quality_m.group(0).upper() if quality_m else ""

    title = base
    for pat in (_YEAR_RE, _QUALITY_RE):
        title = pat.sub(" ", title)
    title = re.sub(r"[\.\-_]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -_|")

    return {
        "title": title or base or "Movie",
        "year": year,
        "quality": quality,
        "full": first or raw,
    }


def correct_spelling(query: str, candidates: Optional[List[str]] = None) -> Optional[str]:
    q = normalize(query)
    if not q or len(q) < 3:
        return None
    if q in COMMON_FIXES:
        return COMMON_FIXES[q]
    if candidates and _HAS_FUZZ:
        try:
            from rapidfuzz import process as _rf_process
            choices = {i: normalize(c) for i, c in enumerate(candidates) if c}
            if choices:
                r = _rf_process.extractOne(q, choices, scorer=_rf_fuzz.WRatio, score_cutoff=82)
                if r:
                    return candidates[r[0]]
        except Exception:  # noqa: BLE001
            pass
    return None


def _fuzzy_score(query: str, candidate: str) -> int:
    qn = normalize(query)
    cn = normalize(candidate)
    if not qn or not cn:
        return 0
    if _HAS_FUZZ:
        try:
            base = int(_rf_fuzz.WRatio(qn, cn))
            # Substring bonus: "toxic" vs "toxic 2024 1080p" should score high
            if qn in cn:
                base = max(base, 92)
            return base
        except Exception:  # noqa: BLE001
            pass
    score = 0
    for tok in _tokens(query):
        if tok in cn:
            score += 15
    if qn in cn:
        score += 40
    return score


def _media_of(msg: Message):
    for attr in _MEDIA_ATTRS:
        media = getattr(msg, attr, None)
        if media is not None:
            return attr, media
    return None, None


def _title_of(msg: Message, media=None) -> str:
    if media is None:
        _, media = _media_of(msg)
    if media is not None:
        for attr in ("file_name", "title", "performer"):
            val = getattr(media, attr, None)
            if val:
                return str(val)
    cap = (getattr(msg, "caption", None) or getattr(msg, "text", None) or "").strip()
    return cap.split("\n")[0][:200] or "Movie"


def _is_playable(msg: Message) -> bool:
    kind, media = _media_of(msg)
    if kind is None:
        return False
    if kind == "document":
        mime = (getattr(media, "mime_type", "") or "").lower()
        return (
            mime.startswith("video") or mime.startswith("audio")
            or mime in ("application/x-matroska", "application/octet-stream")
        )
    return True


def looks_like_name_query(message) -> bool:
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not text or text[:1] not in ("/", "!"):
        return False
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return False
    arg = parts[1].strip()
    if not arg or _URL_RE.match(arg):
        return False
    if getattr(message, "reply_to_message", None) is not None:
        return False
    return True


def query_from_text(message) -> str:
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not text or text[:1] not in ("/", "!"):
        return ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    arg = parts[1].strip()
    if not arg or _URL_RE.match(arg) or len(arg) < 2:
        return ""
    return arg[:120]


def movie_channels() -> List:
    """Ordered list: owner-set channels + env fallback."""
    out: List = []
    try:
        from bot.database.owner_data import get_movie_channels
        for c in (get_movie_channels() or []):
            if c and c not in out:
                out.append(c)
    except Exception:  # noqa: BLE001
        pass
    for attr in ("MOVIE_CHANNEL_ID", "MOVIE_CHANNEL"):
        val = getattr(config, attr, None)
        if val and val not in out:
            out.append(val)
    raw = getattr(config, "MOVIE_CHANNELS", None) or ""
    for c in str(raw).split(","):
        c = c.strip()
        if c:
            c = int(c) if c.lstrip("-").isdigit() else c
            if c not in out:
                out.append(c)
    return out


# Channels the assistant is a member/admin of, found automatically.
_AUTO_CHANNELS: List = []
_AUTO_TS = 0.0
_AUTO_TTL = 30 * 60


async def auto_channels(client=None) -> List:
    """ROOT FIX for "Movie nahi mili" when nobody ran /owner -> Add Channel
    (or the setting was lost on a Heroku restart without Mongo): use every
    channel the assistant account has joined. The owner said the bot and the
    assistant are admins of the movie channel, so it is always in this list.
    """
    global _AUTO_TS
    if _AUTO_CHANNELS and time.time() - _AUTO_TS < _AUTO_TTL:
        return list(_AUTO_CHANNELS)
    cli = client if client is not None else assistant
    found: List = []
    try:
        async for d in cli.get_dialogs():
            chat = getattr(d, "chat", None)
            if chat is not None and chat.type == enums.ChatType.CHANNEL:
                found.append(chat.id)
    except Exception as e:  # noqa: BLE001
        logger.warning("movie_search auto_channels fail: %s", e)
    if found:
        _AUTO_CHANNELS[:] = found
        _AUTO_TS = time.time()
        logger.info("movie_search: auto-detected %d channel(s): %s", len(found), found[:10])
    return list(_AUTO_CHANNELS)


async def all_channels(client=None) -> List:
    """Configured channels FIRST, then every channel the assistant has joined.

    Earlier only configured channels were used when any existed — so one
    stale/wrong id in /owner or MOVIE_CHANNELS hid the real movie channel
    (where bot + assistant are admins) and every search said "nahi mili".
    """
    out: List = list(movie_channels())
    for c in await auto_channels(client):
        if c not in out:
            out.append(c)
    return out[:20]


def not_found_text(query: str) -> str:
    return (
        "🔍 <b>Movie nahi mili</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔎 <b>Query:</b> <code>{html.escape(query[:80])}</code>\n\n"
        "<b>Try karein:</b>\n"
        "• Spelling check karein (<code>/vplay mirzapur</code>)\n"
        "• File par <b>reply</b> karke <code>/vplay</code>\n"
        "• Direct link: <code>/vplay &lt;url&gt;</code>"
    )


# ------------------------------------------------------------ search stages
async def _throttle(key) -> None:
    wait = _MIN_INTERVAL - (time.time() - _LAST_CALL.get(key, 0.0))
    if wait > 0:
        await asyncio.sleep(wait)
    _LAST_CALL[key] = time.time()


async def _live_search_one(client, chat_id, query: str, per_filter: int = 8) -> List[Message]:
    hits = []
    for flt in _FILTERS:
        await _throttle((chat_id, flt))
        try:
            async for msg in client.search_messages(
                chat_id=chat_id, query=query, filter=flt, limit=per_filter
            ):
                if _is_playable(msg):
                    hits.append(msg)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as e:  # noqa: BLE001
            logger.info("movie_search live %s %s: %s", chat_id, flt, e)
    return hits


async def _global_search(client, query: str, limit: int = 20) -> List[Message]:
    if not hasattr(client, "search_global"):
        return []
    await _throttle("global")
    hits = []
    try:
        async for msg in client.search_global(query=query, limit=limit):
            if _is_playable(msg):
                hits.append(msg)
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
    except Exception as e:  # noqa: BLE001
        logger.info("movie_search global: %s", e)
    return hits


# Quality tier for ranking (higher = better).
_QUALITY_RANK = {
    "2160p": 5, "4k": 5, "uhd": 5,
    "1080p": 4, "fhd": 4,
    "720p": 3,
    "480p": 2, "sd": 2,
    "360p": 1,
}

# Sort mode presets: (score, quality, recency) weights.
_SORT_MODES = {
    "latest":   (0.20, 0.20, 0.60),  # DEFAULT — user wants newest
    "balanced": (0.30, 0.35, 0.35),
    "quality":  (0.30, 0.50, 0.20),
    "score":    (0.60, 0.20, 0.20),
}


def _quality_rank(text: str) -> int:
    """Return 0-5 quality tier.

    Explicit resolutions are checked BEFORE the "4k"/"2160p" tokens, because
    group names like "DS4K" would otherwise beat the real "480P" tag in the
    same filename (e.g. "Mirzapur EXTENDED DS4K ... 480P" was ranked 2160p).
    """
    t = normalize(text)
    for q, r in (("1080p", 4), ("720p", 3), ("480p", 2), ("360p", 1)):
        if q in t:
            return r
    for q, r in (("2160p", 5), ("4k", 5), ("uhd", 5), ("1080p", 4), ("fhd", 4)):
        if q in t:
            return r
    if "bluray" in t or "blu ray" in t:
        return 3
    if "webdl" in t or "web dl" in t or "webrip" in t or "hdrip" in t:
        return 2
    if "hdcam" in t or "hdts" in t or "hdtc" in t or "camrip" in t:
        return 1
    return 0

def _weights():
    mode = (getattr(config, "MOVIE_SORT_MODE", "latest") or "latest").lower()
    return _SORT_MODES.get(mode, _SORT_MODES["latest"])


async def _recent_scan(client, chat_id, query: str, limit: int = 200) -> List[Message]:
    """Scan recent media in a chat and fuzzy-match locally.

    Telegram's server-side search matches message text/caption, NOT the file
    name. A movie whose caption is "🍿 New Upload" but filename is
    "TOXiC.2026.720p.mkv" is invisible to search_messages("toxic"). Scanning
    the recent history and scoring the file_name locally catches it.
    """
    hits: List[tuple] = []
    scanned = 0
    try:
        async for msg in client.get_chat_history(chat_id, limit=limit):
            scanned += 1
            if not _is_playable(msg):
                continue
            title = _title_of(msg)
            parsed = parse_caption(title)
            hay = f"{parsed['title']} {parsed.get('year', '')} {title}"
            sc = _fuzzy_score(query, hay)
            if sc >= _MIN_SCORE:
                hits.append((sc, msg))
        hits.sort(key=lambda x: -x[0])
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
    except Exception as e:  # noqa: BLE001
        logger.info("movie_search recent_scan %s: %s", chat_id, e)
    logger.info(
        "movie_search recent_scan chat=%s scanned=%d hits=%d",
        chat_id, scanned, len(hits),
    )
    return [m for _, m in hits]


def _best(query: str, messages: List[Message]) -> Tuple[Optional[Message], int]:
    """Weighted ranking: score + quality + recency.

    Modes (MOVIE_SORT_MODE env):
      latest   - newest upload wins (default, 60% recency)
      balanced - even mix
      quality  - best resolution wins
      score    - best text match wins

    Safety: every candidate MUST pass _MIN_SCORE, so "latest" never plays
    a random movie. Quality tier prevents playing 360p when 1080p exists.
    """
    if not messages:
        return None, 0

    candidates = []
    for msg in messages:
        title = _title_of(msg)
        parsed = parse_caption(title)
        hay = f"{parsed['title']} {parsed.get('year', '')} {title}"
        s = _fuzzy_score(query, hay)
        if s < _MIN_SCORE:
            continue
        qr = _quality_rank(title)
        mid = getattr(msg, "id", 0) or 0
        candidates.append((s, qr, mid, msg))

    if not candidates:
        return None, 0

    max_mid = max(c[2] for c in candidates) or 1
    w_score, w_quality, w_recency = _weights()

    def final_weight(c):
        s, qr, mid, _ = c
        recency = (mid / max_mid) * 100       # 0-100
        quality = qr * 20                     # 0-5 -> 0-100
        return s * w_score + quality * w_quality + recency * w_recency

    candidates.sort(key=final_weight, reverse=True)
    top = candidates[0]
    s, qr, mid, msg = top

    logger.info(
        "movie_search pick: id=%s score=%d quality_tier=%d weight=%.1f "
        "(mode=%s, candidates=%d)",
        mid, s, qr, final_weight(top),
        getattr(config, "MOVIE_SORT_MODE", "latest"), len(candidates),
    )
    return msg, s


# ------------------------------------------------------------ public API
async def search(query: str, client=None) -> Tuple[Optional[Message], str]:
    if not getattr(config, "MOVIE_SEARCH_ENABLED", True):
        return None, ""

    q = (query or "").strip()
    if len(q) < 2:
        return None, ""

    key = normalize(q)
    cached = _CACHE.get(key)
    if cached:
        ts, msg, src = cached
        ttl = _CACHE_TTL if msg is not None else _MISS_TTL
        if time.time() - ts < ttl:
            return msg, src

    cli = client if client is not None else assistant
    channels = await all_channels(cli)
    if not channels:
        logger.warning(
            "movie_search: NO movie channels found — assistant ko movie "
            "channel me add karein. Only global search will run."
        )

    # Spelling correction (apply once, cache both)
    if channels:
        corrected = correct_spelling(q)
        if corrected and corrected != q:
            logger.info("movie_search spelling: %r -> %r", q, corrected)
            q = corrected

    # Stage 1: PARALLEL live search across ALL channels
    if channels:
        try:
            tasks = [_live_search_one(cli, ch, q) for ch in channels]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            merged: List[Message] = []
            for r in results:
                if isinstance(r, list):
                    merged.extend(r)
            best, score = _best(q, merged)
            logger.info(
                "movie_search '%s' channels=%d hits=%d best_score=%d",
                q, len(channels), len(merged), score,
            )
            if best is not None and score >= _MIN_SCORE:
                _CACHE[key] = (time.time(), best, "channel")
                return best, "channel"
        except Exception as e:  # noqa: BLE001
            logger.warning("movie_search channel stage fail: %s", e)

    # Stage 1.5: recent-scan each channel (catches file-name-only matches that
    # Telegram's caption search misses, e.g. "/vplay toxic" when only the
    # file_name contains "TOXiC" but the caption is emoji-only).
    if channels:
        for ch in channels:
            try:
                cand = await _recent_scan(cli, ch, q, limit=_scan_limit())
                best, score = _best(q, cand)
                if best is not None and score >= _MIN_SCORE:
                    _CACHE[key] = (time.time(), best, "channel")
                    logger.info(
                        "movie_search '%s' -> channel-recent (score %d)", q, score
                    )
                    return best, "channel"
            except Exception as e:  # noqa: BLE001
                logger.warning("movie_search recent stage %s: %s", ch, e)

    # Stage 2: global fallback (exact query, then first-token retry for
    # single-word queries that Telegram's global search underserves).
    _global_queries = [q]
    _toks = [t for t in normalize(q).split() if len(t) >= 3]
    if len(_toks) > 1:
        # Multi-word: also try the first token (broader match)
        _global_queries.append(_toks[0])
    elif len(_toks) == 1:
        # ROOT FIX: single common word ("toxic") routinely misses in Telegram's
        # global search because it matches captions only and relevance-limits
        # generic terms. Appending recent years usually unlocks the match
        # ("toxic 2026" worked in testing). Keep the original query first so
        # exact-title users still get their preferred hit.
        from datetime import datetime
        _yr = datetime.now().year
        for _y in (_yr, _yr - 1, _yr - 2):
            _global_queries.append(f"{q} {_y}")

    for _gq in _global_queries:
        try:
            glimit = int(getattr(config, "MOVIE_GLOBAL_LIMIT", 50) or 20)
            best, score = _best(_gq, await _global_search(cli, _gq, glimit))
            if best is not None and score >= _MIN_SCORE:
                _CACHE[key] = (time.time(), best, "global")
                logger.info(
                    "movie_search '%s' -> global (query=%r, score %d)",
                    q, _gq, score,
                )
                return best, "global"
        except Exception as e:  # noqa: BLE001
            logger.warning("movie_search global stage fail: %s", e)

    _CACHE[key] = (time.time(), None, "")
    logger.info("movie_search '%s' -> miss", q)
    return None, ""


def _scan_limit() -> int:
    try:
        return max(200, min(int(getattr(config, "MOVIE_SCAN_LIMIT", 1500) or 1500), 5000))
    except (TypeError, ValueError):
        return 1500


def _query_variants(q: str) -> List[str]:
    """Original query, spelling-fixed query, and each meaningful word.
    Telegram's server search is exact-ish, so a typo ("jailr") or an extra
    word ("jailer movie") misses — single words widen the net and the local
    fuzzy scorer filters the noise back out."""
    out = [q]
    fixed = correct_spelling(q)
    if fixed and fixed not in out:
        out.append(fixed)
    stop = {"movie", "film", "full", "hindi", "dubbed", "hd", "the", "season", "episode", "ep"}
    for t in normalize(q).split():
        if len(t) >= 3 and t not in stop and t not in out:
            out.append(t)
    return out[:5]


async def search_many(query: str, client=None, limit: int = 8) -> List[Message]:
    """Return up to `limit` distinct playable matches, best first.

    Used by /vplay to show a "kaunsa play karein?" button list instead of
    silently auto-picking one result (e.g. /vplay toxic -> 3 TOXIC files).
    """
    q = (query or "").strip()
    if len(q) < 2 or not getattr(config, "MOVIE_SEARCH_ENABLED", True):
        return []
    cli = client if client is not None else assistant
    channels = await all_channels(cli)
    variants = _query_variants(q)
    pool: List[Message] = []

    if channels:
        tasks = [_live_search_one(cli, ch, v) for ch in channels for v in variants]
        for r in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(r, list):
                pool.extend(r)
        # File-name-only uploads are invisible to Telegram search: scan history.
        scans = await asyncio.gather(
            *[_recent_scan(cli, ch, q, limit=_scan_limit()) for ch in channels],
            return_exceptions=True,
        )
        for r in scans:
            if isinstance(r, list):
                pool.extend(r)

    if not pool:
        glimit = int(getattr(config, "MOVIE_GLOBAL_LIMIT", 25) or 25)
        for v in variants[:2]:
            pool.extend(await _global_search(cli, v, glimit))
            if pool:
                break

    ranked = []
    seen = set()
    for msg in pool:
        chat_id = getattr(getattr(msg, "chat", None), "id", None)
        key = (chat_id, getattr(msg, "id", None))
        if key in seen:
            continue
        seen.add(key)
        title = _title_of(msg)
        parsed = parse_caption(title)
        hay = f"{parsed['title']} {parsed.get('year', '')} {title}"
        sc = max(_fuzzy_score(v, hay) for v in variants)
        if sc < _MIN_SCORE:
            continue
        ranked.append((sc, _quality_rank(title), getattr(msg, "id", 0) or 0, msg))
    ranked.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    out = [c[3] for c in ranked[:limit]]
    logger.info("movie_search many %r channels=%d pool=%d results=%d", q, len(channels), len(pool), len(out))
    return out


def result_label(msg: Message) -> str:
    title = _title_of(msg)
    _, media = _media_of(msg)
    size = (getattr(media, "file_size", 0) or 0) / (1024 ** 3)
    p = parse_caption(title)
    label = p["title"][:38]
    if p.get("year"):
        label += f" ({p['year']})"
    if p.get("quality"):
        label += f" {p['quality']}"
    if size:
        label += f" · {size:.1f}GB"
    return label[:60]


def build_track(msg: Message, requested_by: str, requested_by_id: int,
                source: str = "channel") -> Track:
    from bot.utils.extract import _short, _thumb_from_media

    kind, media = _media_of(msg)
    if media is None:
        raise ValueError("Movie message me playable media nahi mila.")

    chat = getattr(msg, "chat", None)
    chat_id = getattr(chat, "id", None) or getattr(msg, "chat_id", None)
    if not chat_id:
        raise ValueError("Movie message ka chat resolve nahi hua.")

    try:
        streamer.remember(msg, assistant)
    except Exception as e:  # noqa: BLE001
        logger.warning("streamer.remember(movie) fail: %s", e)

    parsed = parse_caption(_title_of(msg, media))
    clean_title = parsed["title"] or _title_of(msg, media)
    if parsed.get("year"):
        clean_title = f"{clean_title} ({parsed['year']})"
    if parsed.get("quality"):
        clean_title = f"{clean_title} • {parsed['quality']}"

    mime = (getattr(media, "mime_type", "") or "").lower()
    is_video = kind in ("video", "animation", "video_note") or mime.startswith("video")
    duration = int(getattr(media, "duration", 0) or 0)

    return Track(
        source=get_stream_url(chat_id, msg.id),
        title=_short(clean_title),
        requested_by=requested_by,
        requested_by_id=requested_by_id,
        is_url=True,
        duration=duration,
        mode="channel" if source == "channel" else "global",
        is_video=is_video,
        thumb_id=_thumb_from_media(media),
    )
