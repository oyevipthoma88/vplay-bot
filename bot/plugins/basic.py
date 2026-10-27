from pyrogram.types import Message

from bot import __version__, config
from bot.core.clients import bot
from bot.database import get_stats, set_log_channel
from bot.utils.commands import cmd
from bot.utils.decorators import error_guard

START_TEXT = (
    "<b>Namaste! Main VPlay hoon — video-chat player bot.</b>\n\n"
    "⚡ Koi bhi video/audio, chahe 3 GB ki movie ho, <b>turant (0 sec)</b> stream hoti hai — "
    "pehle download nahi hoti.\n\n"
    "1. Group me video chat start karein\n"
    "2. Assistant ko admin banayein (Manage Video Chats)\n"
    "3. File par reply karke <code>/vplay</code> bhejein\n\n"
    "/help se saari commands dekhein."
)

HELP_TEXT = (
    "<b>🎬 Playback</b>\n"
    "/vplay — file par reply karke ya <code>/vplay &lt;link&gt;</code>\n"
    "/vpause · /vresume · /vskip · /vstop\n"
    "/vseek &lt;sec ya mm:ss&gt; — aage/peeche\n"
    "/vqueue — queue list\n"
    "/vnowplaying — abhi kya chal raha hai\n"
    "/vmute · /vunmute\n\n"
    "<b>🛠 System</b>\n"
    "/stats — chats, users, plays\n"
    "/setlog &lt;chat_id&gt; — log group set (owner)\n"
    "/ping · /vdebug"
)


@bot.on_message(cmd("start"))
@error_guard
async def start_cmd(client, message: Message):
    await message.reply_text(START_TEXT)


@bot.on_message(cmd("help"))
@error_guard
async def help_cmd(client, message: Message):
    await message.reply_text(HELP_TEXT)


@bot.on_message(cmd("ping"))
@error_guard
async def ping_cmd(client, message: Message):
    await message.reply_text(f"🏓 Pong! VPlay v{__version__} chal raha hai.")


@bot.on_message(cmd("stats"))
@error_guard
async def stats_cmd(client, message: Message):
    s = await get_stats()
    await message.reply_text(
        "📊 <b>VPlay Stats</b>\n\n"
        f"🗄 Database: {'✅ MongoDB' if s['is_mongo'] else '⚠️ In-memory (MONGO_DB_URI set karein)'}\n"
        f"👥 Chats: <code>{s['total_chats']}</code>\n"
        f"👤 Users: <code>{s['total_users']}</code>\n"
        f"🎬 Plays: <code>{s['total_plays']}</code>"
    )


@bot.on_message(cmd("setlog"))
@error_guard
async def setlog_cmd(client, message: Message):
    if not message.from_user or message.from_user.id != config.OWNER_ID:
        await message.reply_text("Sirf bot owner ye command use kar sakta hai.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: <code>/setlog -100123456789</code>")
        return
    try:
        channel_id = int(parts[1].strip())
    except ValueError:
        await message.reply_text("Chat ID number hona chahiye.")
        return
    await bot.send_message(channel_id, "✅ VPlay log group set ho gaya.")
    await set_log_channel(channel_id)
    await message.reply_text(f"✅ Log group set: <code>{channel_id}</code>")
