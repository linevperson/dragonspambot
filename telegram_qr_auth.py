import os
import asyncio
from dotenv import load_dotenv
from pyrogram import Client

load_dotenv(".env")

async def main():
    app = Client(
        "dragon_spam_new_account",
        api_id=int(os.environ["API_ID"]),
        api_hash=os.environ["API_HASH"],
        workdir="qr-sessions"
    )

    await app.start(use_qr=True)

    me = await app.get_me()
    print(f"✅ Авторизация успешна: {me.id}")

    await app.stop()

asyncio.run(main())
