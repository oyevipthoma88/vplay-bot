"""Run this ONCE on your own computer (not on Heroku) to log in with the
Telegram account that will act as the assistant/userbot, and get a
SESSION_STRING to paste into your .env / Heroku config vars.

    pip install kurigram tgcrypto
    python3 generate_session.py

It will ask for your phone number, then the login code Telegram sends you
(and your 2FA password, if you have one set). Nothing is uploaded anywhere
— the session string is only printed to your own terminal.
"""
import asyncio

from pyrogram import Client


async def main():
    api_id = int(input("API_ID (my.telegram.org): ").strip())
    api_hash = input("API_HASH (my.telegram.org): ").strip()

    async with Client("vplay-session-gen", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        session_string = await app.export_session_string()

    print("\n" + "=" * 60)
    print("Ye raha aapka SESSION_STRING — ise KISI KE SAATH share mat karein:")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print("\nIse .env ya Heroku config vars mein SESSION_STRING ke naam se daal dein.")


if __name__ == "__main__":
    asyncio.run(main())
