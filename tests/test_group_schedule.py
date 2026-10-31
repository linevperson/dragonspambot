import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import spammer
from database import Database
import handlers
from handlers import format_delay_range


class GroupScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.db = Database(str(self.path))
        await self.db.init_db(1)
        await self.db.save_account(1, 1)
        self.campaign = await self.db.create_campaign("test", 1, 1800, 1800)
        await self.db.add_groups(self.campaign, ["group1", "group2"])
        await self.db.add_message(self.campaign, "hello", "text")
        await self.db.set_campaign_running(self.campaign, True)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_group_intervals_are_independent_and_survive_restart(self):
        groups = await self.db.get_campaign_groups(self.campaign)
        one, two = [group["id"] for group in groups]
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, one, 1000))
        await self.db.finish_group_attempt(self.campaign, one, 1005)
        self.assertFalse(await self.db.reserve_group_attempt(self.campaign, one, 2804))
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, two, 1006))
        await self.db.init_db(1)
        await self.db.set_campaign_running(self.campaign, True)
        self.assertFalse(await self.db.reserve_group_attempt(self.campaign, one, 2804))
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, one, 2805))

    async def test_reservation_prevents_immediate_retry_after_crash(self):
        group_id = (await self.db.get_campaign_groups(self.campaign))[0]["id"]
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, group_id, 1000))
        await self.db.init_db(1)
        await self.db.set_campaign_running(self.campaign, True)
        self.assertFalse(await self.db.reserve_group_attempt(self.campaign, group_id, 1001))

    async def test_changing_interval_recalculates_from_last_attempt(self):
        group_id = (await self.db.get_campaign_groups(self.campaign))[0]["id"]
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, group_id, 1000))
        await self.db.finish_group_attempt(self.campaign, group_id, 1005)
        await self.db.update_campaign_delay(self.campaign, 600, 600)
        self.assertFalse(await self.db.reserve_group_attempt(self.campaign, group_id, 1604))
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, group_id, 1605))

    async def test_first_pass_sends_to_both_groups_without_30_minute_gap(self):
        client = SimpleNamespace(send_message=AsyncMock())
        lock = asyncio.Lock()
        manager = SimpleNamespace(lock=lambda _: lock, get_client=AsyncMock(return_value=client), failed_flood_writes=set())
        sleeper = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", manager), \
                patch.object(spammer.asyncio, "sleep", sleeper), patch.object(spammer.time, "time", return_value=1000):
            with self.assertRaises(asyncio.CancelledError):
                await spammer.run_campaign_spam(self.campaign)
        self.assertEqual(client.send_message.await_count, 2)
        self.assertEqual([call.args[0] for call in sleeper.await_args_list], [10, 10])
        groups = await self.db.get_campaign_groups(self.campaign)
        self.assertEqual([group["next_attempt_at"] for group in groups], [2800, 2800])

    def test_fixed_interval_label(self):
        self.assertEqual(format_delay_range(1800, 1800), "30 мин")

    async def test_single_minute_input_sets_exact_interval(self):
        message = SimpleNamespace(text="30", from_user=SimpleNamespace(id=1), answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={"campaign_id": self.campaign, "delay_unit": "мин"}), clear=AsyncMock())
        with patch.object(handlers, "db", self.db), patch.object(handlers.accounts, "is_connected", AsyncMock(return_value=True)):
            await handlers.config_delay_save(message, state)
        campaign = await self.db.get_campaign(self.campaign)
        self.assertEqual((campaign["min_delay"], campaign["max_delay"]), (1800, 1800))

    async def test_older_group_schema_is_migrated_without_losing_data(self):
        old_path = Path(self.tmp.name) / "old.db"
        with sqlite3.connect(old_path) as conn:
            conn.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, owner_id INTEGER, name TEXT NOT NULL, min_delay INTEGER DEFAULT 60, max_delay INTEGER DEFAULT 120, is_running BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, group_link TEXT NOT NULL, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(campaign_id, group_link))")
            conn.execute("INSERT INTO campaigns (id, owner_id, name) VALUES (1, 1, 'legacy')")
            conn.execute("INSERT INTO groups (id, campaign_id, group_link) VALUES (2, 1, 'legacy-group')")
        old_db = Database(str(old_path))
        await old_db.init_db(1)
        groups = await old_db.get_campaign_groups(1)
        self.assertEqual(groups[0]["group_link"], "legacy-group")
        self.assertEqual(groups[0]["next_attempt_at"], 0)
