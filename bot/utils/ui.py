"""All user-facing cards and keyboards in one place so the look stays
consistent (start screen, help, now-playing card, queue)."""
import html

from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

# Kurigram 2.2+ removed `disable_web_page_preview`; it's now
# `link_preview_options`. Old Pyrogram only knows the old kwarg.
try:
    from pyrogram.types import LinkPreviewOptions

    NO_PREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except ImportError:  # pragma: no cover - very old pyrogram
    NO_PREVIEW = {"disable_web_page_preview": True}

from bot import __version__, config
from bot.core import runtime
from bot.core.queue import ChatState, Track
from bot.utils.formatting import format_duration, progress_bar


# --- Button color styles (Bot API 9.4 / Kurigram Layer 224+) -----------
try:
    from pyrogram.enums import ButtonStyle as _BS
    _HAS_BUTTON_STYLE = True
except ImportError:
    try:
        from pyrogram.types import ButtonStyle as _BS
        _HAS_BUTTON_STYLE = True
    except ImportError:
        _BS = None
        _HAS_BUTTON_STYLE = False


def _url_btn(text, url, style=None):
    """InlineKeyboardButton with URL + optional color."""
    if style is not None and _HAS_BUTTON_STYLE:
        try:
            return Btn(text, url=url, style=style)
        except (TypeError, ValueError, AttributeError):
            pass
    return Btn(text, url=url)


def _style(name: str):
    """Get ButtonStyle enum member by name (PRIMARY/SUCCESS/DANGER), or None."""
    if not _HAS_BUTTON_STYLE or _BS is None:
        return None
    return getattr(_BS, name.upper(), None)


def _btn(text, callback_data, style=None):
    """InlineKeyboardButton with optional color; degrades gracefully."""
    if style is not None and _HAS_BUTTON_STYLE:
        try:
            return Btn(text, callback_data=callback_data, style=style)
        except (TypeError, ValueError, AttributeError):
            pass
    return Btn(text, callback_data=callback_data)



def _volume_bar(vol: int, width: int = 10) -> str:
    """Visual volume bar (0-200 scale)."""
    vol = max(0, min(200, int(vol)))
    filled = round(vol / 200 * width)
    return "█" * filled + "░" * (width - filled)


def _greeting() -> str:
    """Time-aware greeting (IST). Falls back to 'Hey'."""
    try:
        from datetime import datetime, timedelta, timezone
        _h = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).hour
    except Exception:
        return "Hey"
    if 5 <= _h < 12:
        return "Good morning"
    if 12 <= _h < 17:
        return "Good afternoon"
    if 17 <= _h < 21:
        return "Good evening"
    return "Hey"


def _smart_truncate(text: str, max_len: int = 60) -> str:
    """Word-boundary pe truncate — word beech me na kate."""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len - 1].rsplit(" ", 1)[0] or text[:max_len - 1]
    return cut + "…"


_MODE_EMOJI = {
    "stream": "⚡",
    "link": "🔗",
    "file": "📁",
}

_MODE_LABEL = {
    "stream": "⚡ 0s Telegram stream",
    "link": "🔗 Direct link",
    "file": "📁 Local file",
}


# --- Start / help -------------------------------------------------------


def start_text(first_name: str, stats: dict = None) -> str:
    """Premium start card: hero + blockquote pitch + italic tip.

    `stats` kept for backward compatibility but intentionally NOT shown.
    """
    _name = html.escape((first_name or "dost").strip()[:32]) or "dost"
    return "\n".join([
        f"<b>👋 {_greeting()}, {_name}</b>",
        "",
        "<blockquote>🎬 <b>VPlay</b> — group ke video chat me seedha video/audio. "
        "3GB movie bhi <b>0 second</b> me, bina download.</blockquote>",
        "",
        "<i>💡 Kisi bhi video par reply karke <code>/vplay</code> bhejein</i>",
        "",
        f"<i>v{__version__} · ⚡ instant · 🎬 HD · 🔒 admin-safe</i>",
    ])



def start_markup(include_owner: bool = True) -> Markup:
    P = _style("primary")
    G = _style("success")
    rows = []
    if runtime.BOT_USERNAME:
        rows.append([_url_btn(
            "➕ Add to Group",
            f"https://t.me/{runtime.BOT_USERNAME}?startgroup=true"
            "&admin=manage_video_chats+invite_users+delete_messages",
            style=G,
        )])
    rows.append([
        _btn("📖 Commands", "ui_help_main", style=P),
        _btn("🎛 Features", "ui_features", style=P),
    ])
    _update = getattr(config, "UPDATE_CHANNEL", None) or (
        f"https://t.me/{runtime.BOT_USERNAME}" if runtime.BOT_USERNAME else None
    )
    _support = getattr(config, "SUPPORT_GROUP", None) or f"tg://user?id={config.OWNER_ID}"
    _row = []
    if _update:
        _row.append(_url_btn("📢 Updates", _update, style=P))
    _row.append(_url_btn("💬 Support", _support, style=P))
    if _row:
        rows.append(_row)
    if include_owner:
        rows.append([_url_btn("👤 Owner", f"tg://user?id={config.OWNER_ID}", style=P)])
    return Markup(rows)



def features_text() -> str:
    return "\n".join([
        "🎛 <b>VPlay Features</b>",
        "",
        "<blockquote>Sab kuch buttons se — koi command yaad rakhne ki zaroorat nahi.</blockquote>",
        "",
        "<b>▶️ Playback</b>",
        "⚡ <b>0s start</b> — 3GB file bhi turant",
        "⏯ pause / resume · ⏭ skip · ⏹ stop",
        "⏩ smart seek — <code>5m</code> · <code>2h</code> · <code>1:30</code>",
        "",
        "<b>🎛 Controls</b>",
        "🔊 volume 1–200 · 🔁 loop off/track/queue",
        "🔀 shuffle · 📜 live queue · 🔄 refresh",
        "",
        "<b>🔒 Safe by design</b>",
        "admin-only buttons · maintenance mode",
        "per-chat state · zero downloads",
        "",
        "<i>💡 Tip: player card ke buttons se sab control hota hai</i>",
    ])


def features_markup() -> Markup:
    P = _style("primary")
    G = _style("success")
    D = _style("danger")
    rows = []
    if runtime.BOT_USERNAME:
        _share = (
            f"https://t.me/share/url?url=https://t.me/{runtime.BOT_USERNAME}"
            "&text=Try%20VPlay%20%E2%80%94%20instant%20group%20video%20streaming"
        )
        rows.append([_url_btn("🔗 Share VPlay", _share, style=G)])
    rows.append([
        _btn("⬅️ Back", "ui_start", style=P),
        _btn("❌ Close", "ui_close", style=D),
    ])
    return Markup(rows)



def is_owner_button_error(e: BaseException) -> bool:
    low = f"{type(e).__name__} {e}".lower()
    return "button_user_privacy_restricted" in low or "buttonuserprivacyrestricted" in low \
        or "button_user_invalid" in low or "buttonuserinvalid" in low


def group_start_text() -> str:
    return (
        "✅ <b>VPlay ready hai!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎬 Video chat start karein, phir kisi video/audio par reply karke:\n"
        "<code>/vplay</code>\n\n"
        "📖 <b>Quick commands</b>\n"
        "<code>/vplay</code> — play (reply / link)\n"
        "<code>/vpause</code> · <code>/vresume</code>\n"
        "<code>/vseek 90</code> — seek\n"
        "<code>/vskip</code> · <code>/vstop</code>\n"
        "<code>/vqueue</code> · <code>/vloop</code>\n\n"
        "📚 Saari commands ke liye: <code>/help</code>"
    )

HELP_PAGES = {
    "main": (
        "📖 <b>VPlay Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Category chunein 👇",
    ),
    "play": (
        "🎬 <b>Playback Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>▶️ Play</b>\n"
        "<code>/vplay</code> — file/link reply\n"
        "<code>/vplay &lt;url&gt;</code> — direct link\n\n"
        "<b>⏯ Control</b>\n"
        "<code>/vpause</code> · <code>/vresume</code>\n"
        "<code>/vseek 90</code> · <code>/seek 1:30</code>\n"
        "<code>/vmute</code> · <code>/vunmute</code>\n"
        "<code>/vvolume 150</code>\n\n"
        "<b>⏭ Flow</b>\n"
        "<code>/vskip</code> — agla track\n"
        "<code>/vstop</code> — band + leave call"
    ),
    "queue": (
        "📜 <b>Queue Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<code>/vqueue</code> — queue list\n"
        "<code>/vnp</code> — now playing card\n"
        "<code>/vshuffle</code> — shuffle\n"
        "<code>/vloop</code> — loop mode\n\n"
        "<b>Loop modes:</b>\n"
        "🔁 <b>OFF</b> → normal advance\n"
        "🔁 <b>TRACK</b> → current repeat\n"
        "🔁 <b>QUEUE</b> → end me wapas"
    ),
    "system": (
        "🛠 <b>System Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<code>/ping</code> — alive + latency\n"
        "<code>/stats</code> — chats, users, plays\n"
        "<code>/vdebug</code> — permissions check\n"
        "<code>/vcleanup</code> — temp cleanup\n"
        "<code>/setlog &lt;id&gt;</code> — log group (owner)"
    ),
    "setup": (
        "❓ <b>Setup Guide</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1.</b> Bot ko group me add → <b>admin</b>\n"
        "   <i>Permissions: Invite + Manage Video Chats</i>\n\n"
        "<b>2.</b> Assistant bhi admin banayein\n"
        "   <i>Same permissions</i>\n\n"
        "<b>3.</b> Video chat start karein\n\n"
        "<b>4.</b> Video par reply → <code>/vplay</code>\n\n"
        "🆘 <i>Problem? Group me</i> <code>/vdebug</code>"
    ),
}

def help_text(page: str = "main") -> str:
    return HELP_PAGES.get(page, HELP_PAGES["main"])[0]


def help_markup(page: str = "main", show_back_to_start: bool = True) -> Markup:
    P = _style("primary")
    D = _style("danger")
    if page == "main":
        rows = [
            [_btn("🎬 Playback", "ui_help_play", style=P),
             _btn("📜 Queue", "ui_help_queue", style=P)],
            [_btn("🛠 System", "ui_help_system", style=P),
             _btn("❓ Setup", "ui_help_setup", style=P)],
        ]
        if show_back_to_start:
            rows.append([
                _btn("⬅️ Back", "ui_start", style=P),
                _btn("❌ Close", "ui_close", style=D),
            ])
        else:
            rows.append([_btn("❌ Close", "ui_close", style=D)])
        return Markup(rows)
    return Markup([[
        _btn("⬅️ Back", "ui_help_main", style=P),
        _btn("❌ Close", "ui_close", style=D),
    ]])



# --- Player card --------------------------------------------------------


def now_playing_card(state: ChatState, header: str = "🎬 <b>Ab chal raha hai</b>") -> str:
    t: Track = state.current
    elapsed = state.elapsed()
    total = t.duration
    status = "⏸ Paused" if state.is_paused else "▶️ Playing"

    _title = _smart_truncate(t.title, 55)
    _remaining = max(0, total - elapsed) if total else 0
    _percent = f" <b>{int((elapsed / total) * 100)}%</b>" if total else ""
    time_line = (
        f"<code>{format_duration(elapsed)} {progress_bar(elapsed, total)} "
        f"{format_duration(total) if total else 'LIVE'}</code>{_percent}"
        + (f"  <i>⏳ {format_duration(_remaining)} baki</i>" if total else "")
    )

    _mode_e = _MODE_EMOJI.get(t.mode, "📌")
    _pos = f"  ·  <b>1/{1 + len(state.queue)}</b>" if state.queue else ""

    _lm = getattr(state, "loop_mode", "off")
    _vol = getattr(state, "volume", 100)

    _flags = [status]
    if state.is_muted:
        _flags.append("🔇 Muted")
    if _lm == "track":
        _flags.append("🔁 Track")
    elif _lm == "queue":
        _flags.append("🔁 Queue")
    if _vol != 100:
        _flags.append(f"🔊 {_vol}%")

    lines = [
        header,
        "━━━━━━━━━━━━━━━━━━━━",
        f"{_mode_e} <b>{html.escape(_title)}</b>{_pos}",
        time_line,
        "",
        f"{'🎞' if t.is_video else '🎵'} <b>{'Video' if t.is_video else 'Audio'}</b>"
        f"  ·  {_MODE_LABEL.get(t.mode, t.mode)}",
        f"👤 <b>Requested by:</b> {html.escape(t.requested_by)}",
        f"📊 <b>Status:</b> {'  ·  '.join(_flags)}",
    ]
    if state.queue:
        _nxt = html.escape(_smart_truncate(state.queue[0].title, 45))
        _more = f" <i>(+{len(state.queue) - 1} aur)</i>" if len(state.queue) > 1 else ""
        lines.append(f"⏭ <b>Next:</b> {_nxt}{_more}")
    if _vol != 100:
        lines.append(f"🔊 <code>{_volume_bar(_vol)}</code>")
    return "\n".join(lines)

def player_markup(chat_id: int, state: ChatState = None) -> Markup:
    paused = bool(state and state.is_paused)
    muted = bool(state and state.is_muted)
    looping = bool(state and getattr(state, "loop_mode", "off") != "off")
    _lm_label = {"off": "OFF", "track": "TRACK", "queue": "QUEUE"}.get(
        getattr(state, "loop_mode", "off") if state else "off", "OFF"
    )
    c = chat_id

    S_PRIMARY = _style("primary")
    S_SUCCESS = _style("success")
    S_DANGER = _style("danger")

    return Markup([
        [
            _btn("⏪ 10s", f"cb_back_{c}", style=S_PRIMARY),
            _btn("▶️ Resume" if paused else "⏸ Pause",
                 f"cb_{'resume' if paused else 'pause'}_{c}",
                 style=S_PRIMARY),
            _btn("⏩ 10s", f"cb_fwd_{c}", style=S_PRIMARY),
        ],
        [
            _btn("⏭ Skip", f"cb_skip_{c}", style=S_PRIMARY),
            _btn("⏹ Stop", f"cb_stop_{c}", style=S_DANGER),
            _btn(f"🔁 Loop: {_lm_label}", f"cb_loop_{c}",
                 style=S_SUCCESS if looping else S_PRIMARY),
        ],
        [
            _btn("🔊 Unmute" if muted else "🔇 Mute",
                 f"cb_{'unmute' if muted else 'mute'}_{c}",
                 style=S_DANGER if muted else S_PRIMARY),
            _btn("📜 Queue", f"cb_queue_{c}", style=S_PRIMARY),
            _btn("🔄 Refresh", f"cb_refresh_{c}", style=S_PRIMARY),
        ],
        [_btn("❌ Close card", f"cb_close_{c}", style=S_DANGER)],
    ])



def queue_markup(chat_id: int) -> Markup:
    """Compact keyboard for /vqueue view (no player controls)."""
    c = chat_id
    return Markup([
        [
            _btn("🔙 Now Playing", f"cb_refresh_{c}", style=_style("primary")),
            _btn("🔄 Refresh", f"cb_queue_{c}", style=_style("primary")),
        ],
        [_btn("❌ Close", f"cb_close_{c}", style=_style("danger"))],
    ])



def queued_card(track: Track, position: int) -> str:
    dur = f"  ·  ⏱ {format_duration(track.duration)}" if track.duration else ""
    mode_e = _MODE_EMOJI.get(track.mode, "📌")
    _title = _smart_truncate(track.title, 55)
    return (
        f"➕ <b>Queue me add — Position #{position}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{mode_e} <b>{html.escape(_title)}</b>\n"
        f"{'🎞 Video' if track.is_video else '🎵 Audio'}{dur}\n"
        f"👤 <b>Requested by:</b> {html.escape(track.requested_by)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Abhi kuch chal raha hai, ye baad me play hoga.</i>"
    )

def queue_text(state: ChatState) -> str:
    t = state.current
    elapsed = state.elapsed()
    _now_time = (
        f"  <code>{format_duration(elapsed)}/{format_duration(t.duration)}</code>"
        if t.duration else ""
    )
    _lm = getattr(state, "loop_mode", "off")

    lines = [
        "📜 <b>Queue</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"▶️ <b>Now Playing</b>{_now_time}",
        f"   {_MODE_EMOJI.get(t.mode, '📌')} {html.escape(_smart_truncate(t.title, 50))}",
        "",
    ]

    if state.queue:
        lines.append(f"⏭ <b>Up Next</b> ({len(state.queue)})")
        total = 0
        for i, q in enumerate(state.queue[:20], start=1):
            total += q.duration or 0
            _dur = f"  <code>{format_duration(q.duration)}</code>" if q.duration else ""
            _me = _MODE_EMOJI.get(q.mode, "📌")
            _qt = _smart_truncate(q.title, 45)
            lines.append(f"  <b>{i}.</b> {_me} {html.escape(_qt)}{_dur}")
        if len(state.queue) > 20:
            lines.append(f"  <i>…aur {len(state.queue) - 20} tracks</i>")
        _tot = f"  ·  ~{format_duration(total)}" if total else ""
        lines.append("")
        lines.append(f"📊 <b>Total:</b> {len(state.queue)} tracks{_tot}")
    else:
        lines.append("<i>(Queue khali hai)</i>")
        lines.append("")
        lines.append("<i>Naya track add karne ke liye</i> <code>/vplay</code> <i>bhejein.</i>")

    if _lm != "off":
        lines.append(f"🔁 <b>Loop:</b> {_lm.upper()}")
    return "\n".join(lines)


# --- Owner panel --------------------------------------------------------


def owner_panel_text() -> str:
    from bot.database.owner_data import get_start_pic, is_maintenance
    _maint = "🟢 ON" if is_maintenance() else "⚪ OFF"
    _pic = "✅ Set" if get_start_pic() else "❌ Not set"
    _uname = runtime.BOT_USERNAME or "unknown"
    return (
        "👑 <b>Owner Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Bot: @{_uname}\n"
        f"⚙️ Maintenance: {_maint}\n"
        f"🖼 Start Pic: {_pic}\n"
        "\n<i>Action chunein:</i>"
    )


def owner_panel_markup() -> Markup:
    from bot.database.owner_data import get_start_pic, is_maintenance
    _pic_set = bool(get_start_pic())
    _maint = is_maintenance()
    P = _style("primary")
    D = _style("danger")
    return Markup([
        [
            _btn("📊 Stats", "own_stats", style=P),
            _btn("📢 Broadcast", "own_broadcast", style=P),
        ],
        [
            _btn("🖼 Start Pic", "own_start_pic", style=P),
            _btn("🎬 Default Thumb", "own_thumb", style=P),
        ],
        [
            _btn("⚙️ Maintenance", "own_maintenance", style=(D if _maint else P)),
            _btn("📝 Log Group", "own_log_group", style=P),
        ],
        [
            _btn("🔄 Restart", "own_restart", style=P),
            _btn("❌ Close", "own_close", style=D),
        ],
    ])


