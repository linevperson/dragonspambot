"""Per-user Telegram sessions and QR authorization state."""
import asyncio
import os
import sqlite3
import uuid
from pathlib import Path

from pyrogram import Client
from pyrogram.qrlogin import QRLogin

from database import db


class AccountManager:
    def __init__(self):
        self.clients = {}
        self.pending = {}
        self.locks = {}
        self.failed_flood_writes = set()
        self.qr_tasks = {}
        self.root = Path(__file__).resolve().parent

    def lock(self, user_id):
        return self.locks.setdefault(user_id, asyncio.Lock())

    async def is_connected(self, user_id):
        if user_id in self.clients:
            return True
        folder, name = self.session_location(user_id)
        path = folder / f"{name}.session"
        return bool(await db.get_account(user_id)) and path.is_file()

    @staticmethod
    def session_user_id(path):
        if not path.is_file():
            return None
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as conn:
                row = conn.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
                return row[0] if row else None
        except sqlite3.Error:
            return None

    def session_location(self, user_id):
        folder = self.root / "user-sessions"
        name = f"user_{user_id}"
        if self.session_user_id(folder / f"{name}.session") == user_id:
            return folder, name
        admin_id = os.getenv("ADMIN_ID", "")
        legacy_folder = self.root / "qr-sessions"
        legacy_name = "dragon_spam_new_account"
        if admin_id.isdecimal() and int(admin_id) == user_id:
            if self.session_user_id(legacy_folder / f"{legacy_name}.session") == user_id:
                return legacy_folder, legacy_name
        return folder, name

    async def register_existing_admin(self, admin_id):
        if await db.get_account(admin_id):
            return False
        folder, name = self.session_location(admin_id)
        if self.session_user_id(folder / f"{name}.session") != admin_id:
            return False
        await db.save_account(admin_id, admin_id)
        return True

    def new_client(self, user_id):
        folder, name = self.session_location(user_id)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.name == "user-sessions":
            folder.chmod(0o700)
        return Client(name, api_id=int(os.environ["API_ID"]),
                      api_hash=os.environ["API_HASH"], workdir=str(folder), sleep_threshold=0)

    async def activate(self, user_id, client):
        me = await client.get_me()
        if me.id != user_id:
            raise ValueError("account_mismatch")
        client.me = me
        await client.initialize()
        try:
            await db.save_account(user_id, me.id)
        except Exception:
            await client.stop()
            self.pending.pop(user_id, None)
            raise
        self.clients[user_id] = client
        self.pending.pop(user_id, None)
        path = Path(client.workdir) / f"{client.name}.session"
        if path.exists():
            path.chmod(0o600)

    async def begin_qr(self, user_id):
        await self.cancel(user_id)
        client = self.new_client(user_id)
        try:
            if await client.connect():
                await self.activate(user_id, client)
                return None
            self.pending[user_id] = (client, None, None)
            qr = QRLogin(client)
            await qr.recreate()
            return qr
        except Exception as exc:
            if user_id in self.pending:
                await self.cancel(user_id)
            elif client.is_initialized:
                await client.stop()
            elif client.is_connected:
                await client.disconnect()
            if isinstance(exc, ValueError) and str(exc) == "account_mismatch":
                self.quarantine_session(user_id, client)
            raise

    async def password(self, user_id, password):
        client, _, _ = self.pending[user_id]
        await client.check_password(password)
        await self.activate(user_id, client)

    async def cancel(self, user_id):
        task = self.qr_tasks.pop(user_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        entry = self.pending.pop(user_id, None)
        if entry:
            if entry[0].is_initialized:
                await entry[0].stop()
            elif entry[0].is_connected:
                await entry[0].disconnect()

    async def reject_mismatched(self, user_id):
        client = self.pending[user_id][0]
        await self.cancel(user_id)
        self.quarantine_session(user_id, client)

    def quarantine_session(self, user_id, client):
        if client.name != f"user_{user_id}":
            return
        path = Path(client.workdir) / f"user_{user_id}.session"
        if path.exists():
            path.rename(path.with_name(f"{path.name}.mismatch-{uuid.uuid4().hex}"))

    async def get_client(self, user_id):
        if user_id in self.clients:
            return self.clients[user_id]
        if not await db.get_account(user_id):
            return None
        client = self.new_client(user_id)
        try:
            if not await client.connect():
                await client.disconnect()
                return None
            await self.activate(user_id, client)
            return client
        except Exception:
            if client.is_initialized:
                await client.stop()
            elif client.is_connected:
                await client.disconnect()
            raise

    async def disconnect_user(self, user_id):
        await self.cancel(user_id)
        client = self.clients.pop(user_id, None)
        if client:
            if client.is_initialized:
                await client.stop()
            elif client.is_connected:
                await client.disconnect()

    async def close(self):
        for user_id in list(self.pending):
            await self.cancel(user_id)
        for user_id in list(self.clients):
            await self.disconnect_user(user_id)


accounts = AccountManager()
