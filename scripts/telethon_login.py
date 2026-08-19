"""One-time Telethon login (run on a machine where you can receive the code).

1. Get api_id + api_hash from https://my.telegram.org → API development tools.
2. Run:  uv run python scripts/telethon_login.py
3. Enter phone, the code Telegram sends you (and 2FA password if set).
4. Copy the printed TELETHON_SESSION line into your .env (and the VM's).

The StringSession contains the full account login — treat it like a password.
"""

from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # interactive: phone, code, optional 2FA password
    me = await client.get_me()
    print(f"\nLogged in as {me.first_name} (@{me.username}, id={me.id})")
    print("\nAdd these to .env:\n")
    print(f"TELEGRAM_API_ID={api_id}")
    print(f"TELEGRAM_API_HASH={api_hash}")
    print(f"TELETHON_SESSION={client.session.save()}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
