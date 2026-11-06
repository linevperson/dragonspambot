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
        original_groups = self.db.get_campaign_groups
        async def get_groups(cid):
            groups = await original_groups(cid)
            return groups[::-1] if reverse else groups
        client = SimpleNamespace(send_video_note=AsyncMock(side_effect=send))
        lock = asyncio.Lock()
        manager = SimpleNamespace(lock=lambda _: lock, get_client=AsyncMock(return_value=client), failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", manager), \
                patch.object(self.db, "get_campaign_groups", side_effect=get_groups), \
                patch.object(spammer.time, "time", lambda: clock[0]), patch.object(spammer.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await spammer.run_campaign_spam(self.campaign, SimpleNamespace(download=AsyncMock(side_effect=download)))
        return sent

    async def test_regression_each_group_cycles_circles_with_even_group_and_post_counts(self):
        sent = await self.run_sender(8, [1000.0])
        for group in ("group_A", "group_B"):
            self.assertEqual([circle for target, circle in sent if target == group],
                             ["circle_1", "circle_2", "circle_1", "circle_2"] if group == "group_A" else
                             ["circle_2", "circle_1", "circle_2", "circle_1"])

    async def test_group_order_and_additional_groups_do_not_change_rotation(self):
        await self.db.add_groups(self.campaign, ["group_C"])
        sent = await self.run_sender(9, [1000.0], reverse=True)
        self.assertEqual(sent[:3], [("group_A", "circle_1"), ("group_B", "circle_2"), ("group_C", "circle_1")])
        for group in ("group_A", "group_B", "group_C"):
            self.assertEqual([circle for target, circle in sent if target == group],
                             ["circle_2", "circle_1", "circle_2"] if group == "group_B" else
                             ["circle_1", "circle_2", "circle_1"])

    async def test_worker_restart_does_not_restart_post_sequence(self):
        clock = [1000.0]
        self.assertEqual([circle for _, circle in await self.run_sender(2, clock)], ["circle_1", "circle_2"])
        sent = await self.run_sender(2, clock)
        self.assertEqual([circle for _, circle in sent], ["circle_2", "circle_1"])

    async def test_db_reopen_and_manual_start_preserve_rotation(self):
        first, second = [group["id"] for group in self.groups]
        await self.deliver(first)
        await self.db.init_db(1)
        self.db = Database(str(self.path))
        await self.db.start_campaign_fresh(self.campaign)
        self.assertEqual((await self.db.get_next_group_message(self.campaign, first))["id"], self.posts[1])
        self.assertEqual((await self.db.get_next_group_message(self.campaign, second))["id"], self.posts[1])

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
        self.assertEqual(group["rotation_initialized"], 0)

    async def test_arbitrary_post_and_group_counts_follow_formula(self):
        # Counts are test inputs only; production reads both counts from the database.
        for post_count, group_count in ((1, 5), (5, 10), (10, 20), (20, 10), (30, 30), (7, 13)):
            with self.subTest(posts=post_count, groups=group_count):
                cid = await self.db.create_campaign("matrix", 1)
                await self.db.add_groups(cid, [f"matrix_group_{i}" for i in range(group_count)])
                post_ids = []
                kinds = ("text", "photo", "video", "video_note")
                for i in range(post_count):
                    post_ids.append(await self.db.add_message(cid, f"post_{i}", kinds[i % len(kinds)], f"file_{i}"))
                groups = await self.db.get_campaign_groups(cid)
                rounds = post_count + 1 if post_count <= 5 else 4
                for round_index in range(rounds):
                    for group_index, group in enumerate(groups):
                        post = await self.db.get_next_group_message(cid, group["id"])
                        self.assertEqual(post["id"], post_ids[(group_index + round_index) % post_count])
                        await self.db.record_group_message(cid, group["id"], post["id"])

    async def test_ten_groups_five_circles_sender_first_three_rounds(self):
        await self.db.add_groups(self.campaign, [f"group_{i}" for i in range(3, 11)])
        for i in range(3, 6):
            await self.db.add_message(self.campaign, None, "video_note", f"circle_{i}", media_metadata={"duration": 5, "length": 240})
        groups = await self.db.get_campaign_groups(self.campaign)
        sent = await self.run_sender(30, [1000.0])
        expected = [(group["group_link"], f"circle_{(group_index + round_index) % 5 + 1}")
                    for round_index in range(3) for group_index, group in enumerate(groups)]
        self.assertEqual(sent, expected)

    async def test_existing_aligned_queues_are_distributed_once(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE groups SET last_message_id=?, rotation_initialized=0", (self.posts[1],))
        first, second = [group["id"] for group in self.groups]
        self.assertEqual(await self.deliver(first), self.posts[0])
        self.assertEqual(await self.deliver(second), self.posts[1])
        await self.db.init_db(1)
        self.assertEqual(await self.deliver(first), self.posts[1])
        self.assertEqual(await self.deliver(second), self.posts[0])

    async def test_failed_first_attempt_does_not_drop_staggered_start(self):
        second = self.groups[1]["id"]
        for _ in range(3):
            post = await self.db.get_next_group_message(self.campaign, second)
            self.assertEqual(post["id"], self.posts[1])
        self.assertEqual(await self.deliver(second), self.posts[1])
        self.assertEqual(await self.deliver(second), self.posts[0])

    async def test_removing_or_disabling_earlier_group_does_not_shift_initialized_queue(self):
        first, second = [group["id"] for group in self.groups]
        self.assertEqual(await self.deliver(second), self.posts[1])
        await self.db.set_group_enabled(self.campaign, first, 1, False)
        self.assertEqual((await self.db.get_next_group_message(self.campaign, second))["id"], self.posts[0])
        await self.db.delete_group_for_owner(self.campaign, first, 1)
        self.assertEqual(await self.deliver(second), self.posts[0])
        self.assertEqual(await self.deliver(second), self.posts[1])

    async def test_new_post_and_group_use_actual_counts_without_resetting_existing_groups(self):
        first, second = [group["id"] for group in self.groups]
        await self.deliver(first)
        await self.deliver(second)
        third_post = await self.db.add_message(self.campaign, "third", "text")
        await self.db.add_groups(self.campaign, ["group_C"])
        third_group = (await self.db.get_campaign_groups(self.campaign))[-1]["id"]
        self.assertEqual((await self.db.get_next_group_message(self.campaign, first))["id"], self.posts[1])
        self.assertEqual((await self.db.get_next_group_message(self.campaign, second))["id"], third_post)
        self.assertEqual((await self.db.get_next_group_message(self.campaign, third_group))["id"], third_post)
