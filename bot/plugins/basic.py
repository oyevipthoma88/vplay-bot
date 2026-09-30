from pyrogram import filters
from pyrogram.types import Message

from bot import __version__
from bot.core.clients import bot
from bot.utils.decorators import error_guard

START_TEXT = (
    "Namaste! Main ek video-chat player bot hoon.\n\n"
    "Kisi group ke video/voice chat ko start karein, assistant account ko us "
    "group mein admin banayein (Manage Video Chats permission ke saath), "
    "phir kisi video/audio file par reply karke /vplay bhejein — turant "
    "play hone lagega.\n\n"
    "/help se pura command list dekhein."
)

HELP_TEXT = (
    "**Commands**\n\n"
    "/vplay — kisi video/audio par reply karke, ya `/vplay <direct link>` — "
    "play/queue karta hai\n"
    "/vpause — playback pause\n"
    "/vresume — playback resume\n"
    "/vskip — current track skip, queue ka agla track play hoga\n"
    "/vseek <seconds ya mm:ss> — us position par seek\n"
    "/vstop — playback rok kar video chat chhod dega\n"
    "/vqueue — poori queue dikhayega\n"
    "/vnowplaying — abhi kya chal raha hai\n"
    "/vmute — assistant ko mute karega\n"
    "/vunmute — assistant ko unmute karega\n"
    "/ping — bot alive hai ya nahi check\n\n"
    "Note: sirf direct links ya khud bheji hui files chalti hain — koi "
    "YouTube search/cookies wagera nahi chahiye."
)


@bot.on_message(filters.command("start"))
@error_guard
async def start_cmd(client, message: Message):
    await message.reply_text(START_TEXT)


@bot.on_message(filters.command("help"))
@error_guard
async def help_cmd(client, message: Message):
    await message.reply_text(HELP_TEXT)


@bot.on_message(filters.command("ping"))
@error_guard
async def ping_cmd(client, message: Message):
    await message.reply_text(f"Pong! Bot chal raha hai (v{__version__}).")
