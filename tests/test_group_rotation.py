import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import spammer
from database import Database


class GroupRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.db = Database(str(self.path))
        await self.db.init_db(1)
        await self.db.save_account(1, 1)
        self.campaign = await self.db.create_campaign("rotation", 1, 100, 100)
        await self.db.add_groups(self.campaign, ["group_A", "group_B"])
        self.groups = await self.db.get_campaign_groups(self.campaign)
        self.posts = [await self.db.add_message(self.campaign, None, "video_note", f"circle_{i}",
                      media_metadata={"duration": 5, "length": 240}) for i in range(1, 3)]
        await self.db.start_campaign_fresh(self.campaign)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def deliver(self, group_id):
        message = await self.db.get_next_group_message(self.campaign, group_id)
        await self.db.record_group_message(self.campaign, group_id, message["id"])
        return message["id"]

    async def run_sender(self, count, clock, reverse=False, failure=False):
        sent = []
        async def download(file_id, destination):
            destination.write(file_id.encode())
        async def send(**kwargs):
            if failure:
                raise RuntimeError("simulated delivery failure")
            sent.append((kwargs["chat_id"], kwargs["video_note"].read().decode()))
        async def sleep(seconds):
            if len(sent) >= count or failure:
                raise asyncio.CancelledError
            clock[0] += seconds
        def shuffle(items):
            if reverse:
                items.reverse()
        client = SimpleNamespace(send_video_note=AsyncMock(side_effect=send))
        lock = asyncio.Lock()
        manager = SimpleNamespace(lock=lambda _: lock, get_client=AsyncMock(return_value=client), failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", manager), \
                patch.object(spammer.random, "shuffle", shuffle), \
                patch.object(spammer.time, "time", lambda: clock[0]), patch.object(spammer.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await spammer.run_campaign_spam(self.campaign, SimpleNamespace(download=AsyncMock(side_effect=download)))
        return sent

    async def test_regression_each_group_cycles_circles_with_even_group_and_post_counts(self):
        sent = await self.run_sender(8, [1000.0])
        for group in ("group_A", "group_B"):
            self.assertEqual([circle for target, circle in sent if target == group],
                             ["circle_1", "circle_2", "circle_1", "circle_2"])

    async def test_group_order_and_additional_groups_do_not_change_rotation(self):
        await self.db.add_groups(self.campaign, ["group_C"])
        sent = await self.run_sender(9, [1000.0], reverse=True)
        for group in ("group_A", "group_B", "group_C"):
            self.assertEqual([circle for target, circle in sent if target == group],
                             ["circle_1", "circle_2", "circle_1"])

    async def test_worker_restart_does_not_restart_post_sequence(self):
        clock = [1000.0]
        self.assertEqual([circle for _, circle in await self.run_sender(2, clock)], ["circle_1", "circle_1"])
        sent = await self.run_sender(2, clock)
        self.assertEqual([circle for _, circle in sent], ["circle_2", "circle_2"])

    async def test_db_reopen_and_manual_start_preserve_rotation(self):
        first, second = [group["id"] for group in self.groups]
        await self.deliver(first)
        await self.db.init_db(1)
        self.db = Database(str(self.path))
        await self.db.start_campaign_fresh(self.campaign)
        self.assertEqual((await self.db.get_next_group_message(self.campaign, first))["id"], self.posts[1])
        self.assertEqual((await self.db.get_next_group_message(self.campaign, second))["id"], self.posts[0])

    async def test_failure_does_not_advance_rotation_but_keeps_interval(self):
        await self.run_sender(1, [1000.0], failure=True)
        group = await self.db.get_group(self.campaign, self.groups[0]["id"])
        self.assertIsNone(group["last_message_id"])
        self.assertEqual(group["next_attempt_at"], 1100)
        self.assertEqual((await self.db.get_next_group_message(self.campaign, group["id"]))["id"], self.posts[0])

    async def test_deleting_last_sent_post_does_not_rewind_sequence(self):
        group = self.groups[0]["id"]
        await self.deliver(group)
        await self.db.delete_campaign_message(self.campaign, self.posts[0])
        self.assertEqual(await self.deliver(group), self.posts[1])
        self.assertEqual(await self.deliver(group), self.posts[1])  # One surviving variant.

    async def test_deleting_next_and_adding_or_editing_posts(self):
        group = self.groups[0]["id"]
        await self.deliver(group)
        third = await self.db.add_message(self.campaign, "third", "text")
        await self.db.delete_campaign_message(self.campaign, self.posts[1])
        self.assertEqual(await self.deliver(group), third)
        await self.db.replace_message(self.campaign, self.posts[0], "edited", "text", None, "[]", {})
        next_post = await self.db.get_next_group_message(self.campaign, group)
        self.assertEqual((next_post["id"], next_post["text"]), (self.posts[0], "edited"))

    async def test_disabled_group_and_foreign_campaign_do_not_affect_cursor(self):
        first, second = [group["id"] for group in self.groups]
        await self.deliver(first)
        await self.db.set_group_enabled(self.campaign, first, 1, False)
        self.assertIsNone(await self.db.get_next_group_message(self.campaign, first))
        await self.deliver(second)
        await self.db.set_group_enabled(self.campaign, first, 1, True)
        self.assertEqual((await self.db.get_next_group_message(self.campaign, first))["id"], self.posts[1])
        other = await self.db.create_campaign("other", 2)
        self.assertIsNone(await self.db.get_next_group_message(other, first))
        await self.db.record_group_message(other, first, 999)
        self.assertEqual((await self.db.get_group(self.campaign, first))["last_message_id"], self.posts[0])

    async def test_empty_campaign_returns_no_post(self):
        await self.db.clear_campaign_messages(self.campaign)
        self.assertIsNone(await self.db.get_next_group_message(self.campaign, self.groups[0]["id"]))

    async def test_migration_adds_cursor_without_losing_group_schedule(self):
        path = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY, campaign_id INTEGER, group_link TEXT, last_attempt_at REAL, next_attempt_at REAL)")
            conn.execute("INSERT INTO groups VALUES (1, 1, 'legacy', 1000, 2800)")
        legacy = Database(str(path))
        await legacy.init_db(1)
        await legacy.init_db(1)
        group = await legacy.get_group(1, 1)
        self.assertEqual((group["group_link"], group["next_attempt_at"]), ("legacy", 2800))
        self.assertIsNone(group["last_message_id"])
