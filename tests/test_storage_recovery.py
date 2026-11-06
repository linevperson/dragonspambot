import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from campaign_status import format_campaign_status
from database import Database
from storage_recovery import CampaignStorage


class StorageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_is_bounded_and_notifications_are_per_outage(self):
        waiting = set()
        on_wait, on_recovered = AsyncMock(), AsyncMock()
        database = SimpleNamespace(complete_group_attempt=AsyncMock(side_effect=[
            sqlite3.OperationalError("locked") for _ in range(5)
        ] + [None]))
        storage = CampaignStorage(database, 7, waiting, on_wait, on_recovered)
        with patch("storage_recovery.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await storage.complete_group_attempt(7, 2, 1000, 9)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [5, 10, 20, 30, 30])
        self.assertIn(7, waiting)
        on_wait.assert_awaited_once()
        on_recovered.assert_not_awaited()
        await storage.checkpoint()
        self.assertNotIn(7, waiting)
        on_recovered.assert_awaited_once()

    async def test_cancelled_worker_retains_write_until_next_flush(self):
        database = SimpleNamespace(complete_group_attempt=AsyncMock(side_effect=sqlite3.OperationalError("locked")))
        storage = CampaignStorage(database, 7, set(), AsyncMock(), AsyncMock())
        with patch("storage_recovery.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await storage.complete_group_attempt(7, 2, 1000, 9)
        self.assertEqual(storage.pending, [("complete_group_attempt", (7, 2, 1000, 9), {})])
        database.complete_group_attempt = AsyncMock()
        await storage.flush()
        database.complete_group_attempt.assert_awaited_once_with(7, 2, 1000, 9)
        self.assertEqual(storage.pending, [])

    async def test_read_recovery_does_not_enqueue_or_retry_send(self):
        campaign = {"id": 7, "owner_id": 42, "is_running": 1}
        database = SimpleNamespace(get_campaign=AsyncMock(side_effect=[sqlite3.OperationalError("locked"), campaign]))
        storage = CampaignStorage(database, 7, set(), AsyncMock(), AsyncMock())
        with patch("storage_recovery.asyncio.sleep", AsyncMock()):
            self.assertEqual(await storage.get_campaign(7), campaign)
        self.assertEqual(storage.pending, [])
        self.assertEqual(storage.campaign, campaign)

    async def test_completion_commits_cursor_and_interval_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            db = Database(str(path))
            await db.init_db(42)
            cid = await db.create_campaign("atomic", 42, 30, 30)
            await db.add_groups(cid, ["group"])
            mid = await db.add_message(cid, "post", "text")
            gid = (await db.get_campaign_groups(cid))[0]["id"]
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TRIGGER fail_cursor BEFORE UPDATE OF last_message_id ON groups BEGIN SELECT RAISE(ABORT, 'failed'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                await db.complete_group_attempt(cid, gid, 1000, mid)
            group = await db.get_group(cid, gid)
            self.assertEqual(group["next_attempt_at"], 0)
            self.assertIsNone(group["last_message_id"])
            with sqlite3.connect(path) as conn:
                conn.execute("DROP TRIGGER fail_cursor")
            await db.complete_group_attempt(cid, gid, 1000, mid)
            group = await db.get_group(cid, gid)
            self.assertEqual((group["next_attempt_at"], group["last_message_id"]), (1030, mid))

    def test_status_reports_waiting_not_full_stop(self):
        snapshot = {"is_running": 1, "flood_until": 0, "last_attempt_at": 1000}
        text = format_campaign_status(snapshot, storage_waiting=True)
        self.assertIn("Техническое ожидание", text)
        self.assertIn("автоматически", text)
        self.assertNotIn("Остановлена", text)
