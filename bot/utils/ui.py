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
                 Btn("❓ Setup guide", callback_data="ui_help_setup")])
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
            [Btn("🎬 Playback", callback_data="ui_help_play"),
             Btn("📜 Queue", callback_data="ui_help_queue")],
            [Btn("🛠 System", callback_data="ui_help_system"),
             Btn("❓ Setup", callback_data="ui_help_setup")],
        ]
        if show_back_to_start:
            rows.append([Btn("⬅️ Back", callback_data="ui_start")])
        return Markup(rows)
    return Markup([[Btn("⬅️ Back", callback_data="ui_help_main"),
                    Btn("❌ Close", callback_data="ui_close")]])


# --- Player card --------------------------------------------------------


def now_playing_card(state: ChatState, header: str = "🎬 <b>Ab chal raha hai</b>") -> str:
    t: Track = state.current
    elapsed = state.elapsed()
    total = t.duration
    status = "⏸ Paused" if state.is_paused else "▶️ Playing"
    flags = []
    if state.is_muted:
        flags.append("🔇 Muted")
    if state.loop:
        flags.append("🔁 Loop")

    _title = t.title if len(t.title) <= 60 else t.title[:57] + "…"
    _remaining = max(0, total - elapsed) if total else 0
    _percent = f"  <b>{int((elapsed / total) * 100)}%</b>" if total else ""
    time_line = (
        f"<code>{format_duration(elapsed)} {progress_bar(elapsed, total)} "
        f"{format_duration(total) if total else 'LIVE'}</code>{_percent}"
        + (f"  <i>⏳ {format_duration(_remaining)} baki</i>" if total else "")
    )

    lines = [
        header,
        "",
        f"📌 <b>{html.escape(_title)}</b>",
        time_line,
        "",
        f"{'🎞' if t.is_video else '🎵'} <b>Type:</b> {'Video' if t.is_video else 'Audio'}"
        f"  ·  {_MODE_LABEL.get(t.mode, t.mode)}",
        f"👤 <b>By:</b> {html.escape(t.requested_by)}",
        f"📊 <b>Status:</b> {status}" + (f"  ·  {'  ·  '.join(flags)}" if flags else ""),
    ]
    if state.queue:
        _nxt = html.escape(state.queue[0].title)
        if len(state.queue) > 1:
            lines.append(f"⏭ <b>Next:</b> {_nxt} (+{len(state.queue) - 1} aur)")
        else:
            lines.append(f"⏭ <b>Next:</b> {_nxt}")
    return "\n".join(lines)

def player_markup(chat_id: int, state: ChatState = None) -> Markup:
    paused = bool(state and state.is_paused)
    muted = bool(state and state.is_muted)
    looping = bool(state and state.loop)
    c = chat_id
    return Markup([
        [
            Btn("⏪ 10s", callback_data=f"cb_back_{c}"),
            Btn("▶️ Resume" if paused else "⏸ Pause",
                callback_data=f"cb_{'resume' if paused else 'pause'}_{c}"),
            Btn("⏩ 10s", callback_data=f"cb_fwd_{c}"),
        ],
        [
            Btn("⏭ Skip", callback_data=f"cb_skip_{c}"),
            Btn("⏹ Stop", callback_data=f"cb_stop_{c}"),
            Btn("🔁 Loop ✓" if looping else "🔁 Loop", callback_data=f"cb_loop_{c}"),
        ],
        [
            Btn("🔊 Unmute" if muted else "🔇 Mute",
                callback_data=f"cb_{'unmute' if muted else 'mute'}_{c}"),
            Btn("📜 Queue", callback_data=f"cb_queue_{c}"),
            Btn("🔄", callback_data=f"cb_refresh_{c}"),
        ],
        [Btn("❌ Close", callback_data=f"cb_close_{c}")],
    ])


def queued_card(track: Track, position: int) -> str:
    dur = f" · ⏱ {format_duration(track.duration)}" if track.duration else ""
    return (
        f"➕ <b>Queue me add — #{position}</b>\n\n"
        f"📌 <b>{html.escape(track.title)}</b>{dur}\n"
        f"👤 <b>By:</b> {html.escape(track.requested_by)}"
    )


def queue_text(state: ChatState) -> str:
    t = state.current
    lines = [
        "📜 <b>Queue</b>\n",
        f"▶️ <b>Now:</b> {html.escape(t.title)}"
        + (f"  <code>{format_duration(state.elapsed())}/{format_duration(t.duration)}</code>"
           if t.duration else ""),
        "",
    ]
    total = 0
    for i, q in enumerate(state.queue[:20], start=1):
        total += q.duration or 0
        dur = f" · {format_duration(q.duration)}" if q.duration else ""
        lines.append(f"<b>{i}.</b> {html.escape(q.title)}{dur} — <i>{html.escape(q.requested_by)}</i>")
    if len(state.queue) > 20:
        lines.append(f"…aur {len(state.queue) - 20} tracks")
    if not state.queue:
        lines.append("<i>(queue khali hai)</i>")
    else:
        lines.append(f"\n🧮 {len(state.queue)} tracks" + (f" · ~{format_duration(total)}" if total else ""))
    if state.loop:
        lines.append("🔁 Loop ON")
    return "\n".join(lines)
