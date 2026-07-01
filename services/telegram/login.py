"""One-time interactive login. Run this once to mint a Telethon StringSession,
then paste the printed value into .env as TG_SESSION.

    docker compose run --rm telegram python login.py
"""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        print("\n=== Logged in as", me.username or me.first_name, "===")
        print("\nAdd this to your .env as TG_SESSION (single line):\n")
        print(client.session.save())
        print()


if __name__ == "__main__":
    asyncio.run(main())
