import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pyrogram.errors import FloodWait, SlowmodeWait

import spammer
from database import Database


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        await self.db.init_db(1)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def campaign(self, owner):
        await self.db.save_account(owner, owner)
        campaign_id = await self.db.create_campaign("test", owner, 1, 1)
        await self.db.add_message(campaign_id, "hello", "text")
        await self.db.add_groups(campaign_id, ["group1", "group2"])
        await self.db.set_campaign_running(campaign_id, True)
        return campaign_id

    async def test_revocation_and_startup_stop_campaigns(self):
        await self.db.add_authorized_user(2)
        own = await self.campaign(1)
        other = await self.campaign(2)
        await self.db.remove_authorized_user(2)
        self.assertTrue((await self.db.get_campaign(own))["is_running"])
        self.assertFalse((await self.db.get_campaign(other))["is_running"])
        await self.db.init_db(1)
        self.assertFalse((await self.db.get_campaign(own))["is_running"])

    async def test_delete_cascades(self):
        campaign_id = await self.campaign(1)
        await self.db.delete_campaign(campaign_id)
        self.assertEqual(await self.db.get_campaign_messages(campaign_id), [])
        self.assertEqual(await self.db.get_campaign_groups(campaign_id), [])

    async def test_users_see_only_their_campaigns(self):
        first = await self.campaign(1)
        second = await self.campaign(2)
        self.assertEqual([item["id"] for item in await self.db.get_all_campaigns(1)], [first])
        self.assertEqual([item["id"] for item in await self.db.get_all_campaigns(2)], [second])
        self.assertFalse(await self.db.campaign_belongs_to(second, 1))

    async def test_group_selection_checks_owner_atomically(self):
        campaign_id = await self.campaign(1)
        self.assertIsNone(await self.db.add_group_for_owner(campaign_id, 2, "-100123"))
        self.assertTrue(await self.db.add_group_for_owner(campaign_id, 1, "-100123"))
        self.assertFalse(await self.db.add_group_for_owner(campaign_id, 1, "-100123"))
        await self.db.delete_campaign(campaign_id)
        self.assertIsNone(await self.db.add_group_for_owner(campaign_id, 1, "-100123"))

    async def test_flood_stops_only_affected_account_and_persists_deadline(self):
        first = await self.campaign(1)
        second = await self.campaign(1)
        other = await self.campaign(2)
        fake_bot = type("Bot", (), {})()
        fake_bot.send_message = AsyncMock(side_effect=FloodWait(60))
        lock = asyncio.Lock()
        manager = SimpleNamespace(lock=lambda _: lock, get_client=AsyncMock(return_value=fake_bot), failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", manager):
            await asyncio.gather(spammer.run_campaign_spam(first), spammer.run_campaign_spam(second))
        self.assertEqual(fake_bot.send_message.await_count, 1)
        self.assertFalse((await self.db.get_campaign(first))["is_running"])
        self.assertFalse((await self.db.get_campaign(second))["is_running"])
        self.assertTrue((await self.db.get_campaign(other))["is_running"])
        self.assertGreater(await self.db.account_flood_remaining(1), 0)
        self.assertEqual(await self.db.account_flood_remaining(2), 0)
        await self.db.init_db(1)
        self.assertGreater(await self.db.account_flood_remaining(1), 0)

    async def test_error_still_waits_before_next_group(self):
        for error in (SlowmodeWait(5), RuntimeError("failure")):
            campaign_id = await self.campaign(1)
            fake_bot = type("Bot", (), {})()
            fake_bot.send_message = AsyncMock(side_effect=error)
            sleeper = AsyncMock(side_effect=asyncio.CancelledError)
            lock = asyncio.Lock()
            manager = SimpleNamespace(lock=lambda _: lock, get_client=AsyncMock(return_value=fake_bot), failed_flood_writes=set())
            with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", manager), \
                    patch.object(spammer.asyncio, "sleep", sleeper):
                with self.assertRaises(asyncio.CancelledError):
                    await spammer.run_campaign_spam(campaign_id)
            self.assertEqual(fake_bot.send_message.await_count, 1)
            sleeper.assert_awaited_once_with(1)

    async def test_flood_storage_failure_blocks_further_sends(self):
        campaign_id = await self.campaign(1)
        fake_bot = type("Bot", (), {})()
        fake_bot.send_message = AsyncMock(side_effect=FloodWait(60))
        lock = asyncio.Lock()
        manager = SimpleNamespace(lock=lambda _: lock, get_client=AsyncMock(return_value=fake_bot), failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", manager), \
                patch.object(self.db, "stop_account_for_flood", AsyncMock(side_effect=RuntimeError("db failure"))):
            await spammer.run_campaign_spam(campaign_id)
            self.assertGreater(await spammer.flood_wait_remaining(1), 0)
