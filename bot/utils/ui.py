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


def start_text(first_name: str) -> str:
    return (
        f"👋 <b>Namaste {html.escape(first_name or 'dost')}!</b>\n\n"
        "Main <b>VPlay</b> hoon — group ke <b>video chat</b> me seedha video aur audio "
        "chalane wala bot.\n\n"
        "⚡ <b>0 second start</b> — 3 GB movie bhi bina download turant chalti hai\n"
        "🎛 <b>Full control</b> — pause, skip, seek, loop, queue\n"
        "🔒 <b>Admin only</b> control (chahein to band karein)\n\n"
        "<b>Shuru kaise karein?</b>\n"
        "1️⃣ Mujhe group me add karke admin banayein\n"
        "2️⃣ Video chat start karein\n"
        "3️⃣ Kisi video par reply karke <code>/vplay</code> bhejein\n\n"
        f"<i>VPlay v{__version__}</i>"
    )


def start_markup(include_owner: bool = True) -> Markup:
    rows = []
    if runtime.BOT_USERNAME:
        rows.append([Btn(
            "➕ Group me add karein",
            url=f"https://t.me/{runtime.BOT_USERNAME}?startgroup=true"
                "&admin=manage_video_chats+invite_users+delete_messages",
        )])
    rows.append([Btn("📖 Commands", callback_data="ui_help_main"),
                 _btn("❓ Setup guide", "ui_help_setup", style=_style("primary"))])
    if include_owner:
        # Telegram rejects the whole message (BUTTON_USER_PRIVACY_RESTRICTED)
        # if the owner's privacy hides them — callers retry without it.
        rows.append([Btn("👤 Owner", url=f"tg://user?id={config.OWNER_ID}")])
    return Markup(rows)


def is_owner_button_error(e: BaseException) -> bool:
    low = f"{type(e).__name__} {e}".lower()
    return "button_user_privacy_restricted" in low or "buttonuserprivacyrestricted" in low \
        or "button_user_invalid" in low or "buttonuserinvalid" in low


def group_start_text() -> str:
    return (
        "✅ <b>VPlay ready hai!</b>\n\n"
        "Video chat start karein aur kisi video/audio par reply karke "
        "<code>/vplay</code> bhejein.\n/help — saari commands"
    )


HELP_PAGES = {
    "main": (
        "📖 <b>VPlay Commands</b>\n\nCategory chunein 👇",
    ),
    "play": (
        "🎬 <b>Playback</b>\n\n"
        "<code>/vplay</code> — file par reply karke\n"
        "<code>/vplay &lt;link&gt;</code> — direct video/audio link\n"
        "<code>/vpause</code> · <code>/vresume</code>\n"
        "<code>/vskip</code> — agla track\n"
        "<code>/vstop</code> — band + video chat chhodo\n"
        "<code>/vseek 90</code> ya <code>/vseek 1:30</code>\n"
        "<code>/vmute</code> · <code>/vunmute</code>",
    ),
    "queue": (
        "📜 <b>Queue</b>\n\n"
        "<code>/vqueue</code> — queue list\n"
        "<code>/vnp</code> — abhi kya chal raha hai (player card)\n"
        "<code>/vloop</code> — current track repeat ON/OFF\n"
        "<code>/vshuffle</code> — queue shuffle",
    ),
    "system": (
        "🛠 <b>System</b>\n\n"
        "<code>/ping</code> — bot alive check\n"
        "<code>/stats</code> — chats, users, plays\n"
        "<code>/vdebug</code> — bot/assistant permissions check\n"
        "<code>/setlog &lt;chat_id&gt;</code> — log group (owner)",
    ),
    "setup": (
        "❓ <b>Setup guide</b>\n\n"
        "1. Bot ko group me add karke <b>admin</b> banayein "
        "(Invite users + Manage Video Chats)\n"
        "2. Assistant account khud join ho jayega. Use bhi <b>admin</b> banayein "
        "with <b>Manage Video Chats</b>\n"
        "3. Video chat start karein (ya assistant admin ho to wo khud start karega)\n"
        "4. Video par reply karke <code>/vplay</code>\n\n"
        "Problem? Group me <code>/vdebug</code> bhejein.",
    ),
}


def help_text(page: str = "main") -> str:
    return HELP_PAGES.get(page, HELP_PAGES["main"])[0]


def help_markup(page: str = "main", show_back_to_start: bool = True) -> Markup:
    if page == "main":
        rows = [
            [_btn("🎬 Playback", "ui_help_play", style=_style("primary")),
             _btn("📜 Queue", "ui_help_queue", style=_style("primary"))],
            [_btn("🛠 System", "ui_help_system", style=_style("primary")),
             _btn("❓ Setup", "ui_help_setup", style=_style("primary"))],
        ]
        if show_back_to_start:
            rows.append([_btn("⬅️ Back", "ui_start", style=_style("primary"))])
        return Markup(rows)
    return Markup([[_btn("⬅️ Back", "ui_help_main", style=_style("primary")),
                    _btn("❌ Close", "ui_close", style=_style("danger"))]])


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

    # Color scheme: primary (blue) = play/pause; danger (red) = stop/close;
    # success (green) = loop ON. Others default grey.
    S_PRIMARY = _style("primary")
    S_SUCCESS = _style("success")
    S_DANGER = _style("danger")

    return Markup([
        [
            _btn("⏪ 30s", f"cb_back30_{c}"),
            _btn("⏪ 10s", f"cb_back_{c}"),
            _btn("▶️ Resume" if paused else "⏸ Pause",
                 f"cb_{'resume' if paused else 'pause'}_{c}",
                 style=S_PRIMARY),
            _btn("⏩ 10s", f"cb_fwd_{c}"),
            _btn("⏩ 30s", f"cb_fwd30_{c}"),
        ],
        [
            _btn("⏭ Skip", f"cb_skip_{c}"),
            _btn("⏹ Stop", f"cb_stop_{c}", style=S_DANGER),
            _btn(f"🔁 Loop: {_lm_label}", f"cb_loop_{c}",
                 style=S_SUCCESS if looping else None),
        ],
        [
            _btn("🔊 Unmute" if muted else "🔇 Mute",
                 f"cb_{'unmute' if muted else 'mute'}_{c}"),
            _btn("📜 Queue", f"cb_queue_{c}"),
            _btn("🔄 Refresh", f"cb_refresh_{c}"),
        ],
        [_btn("❌ Close card", f"cb_close_{c}", style=S_DANGER)],
    ])


def queue_markup(chat_id: int) -> Markup:
    """Compact keyboard for /vqueue view (no player controls)."""
    c = chat_id
    return Markup([
        [
            _btn("🔙 Now Playing", f"cb_refresh_{c}", style=_style("primary")),
            _btn("🔄 Refresh", f"cb_queue_{c}"),
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

