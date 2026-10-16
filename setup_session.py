import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nСкопируй строку ниже в переменную TELETHON_SESSION на хостинге.")
        print("Не отправляй её никому: она даёт доступ к твоему аккаунту.\n")
        print(client.session.save())

if __name__ == "__main__":
    asyncio.run(main())
