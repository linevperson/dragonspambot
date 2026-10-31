import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import accounts as accounts_module


class AdminSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = accounts_module.AccountManager()
        self.manager.root = self.root
        self.fake_db = SimpleNamespace(get_account=AsyncMock(return_value=None), save_account=AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    def write_session(self, directory, name, user_id):
        directory.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(directory / f"{name}.session") as conn:
            conn.execute("CREATE TABLE sessions (user_id INTEGER)")
            conn.execute("INSERT INTO sessions VALUES (?)", (user_id,))

    async def test_existing_shared_session_binds_only_to_matching_admin(self):
        self.write_session(self.root / "qr-sessions", "dragon_spam_new_account", 42)
        self.write_session(self.root / "user-sessions", "user_42", None)
        with patch.dict(os.environ, {"ADMIN_ID": "42"}), patch.object(accounts_module, "db", self.fake_db):
            self.assertTrue(await self.manager.register_existing_admin(42))
            self.assertEqual(self.manager.session_location(42), (self.root / "qr-sessions", "dragon_spam_new_account"))
            self.assertEqual(self.manager.session_location(99), (self.root / "user-sessions", "user_99"))
        self.fake_db.save_account.assert_awaited_once_with(42, 42)
        self.assertTrue((self.root / "user-sessions" / "user_42.session").exists())

    async def test_different_account_is_not_registered(self):
        self.write_session(self.root / "qr-sessions", "dragon_spam_new_account", 99)
        with patch.dict(os.environ, {"ADMIN_ID": "42"}), patch.object(accounts_module, "db", self.fake_db):
            self.assertFalse(await self.manager.register_existing_admin(42))
        self.fake_db.save_account.assert_not_awaited()

    async def test_matching_per_user_session_takes_precedence(self):
        self.write_session(self.root / "qr-sessions", "dragon_spam_new_account", 42)
        self.write_session(self.root / "user-sessions", "user_42", 42)
        with patch.dict(os.environ, {"ADMIN_ID": "42"}):
            self.assertEqual(self.manager.session_location(42), (self.root / "user-sessions", "user_42"))
