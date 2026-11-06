"""Audit the actual sender with a virtual clock and mocked Telegram requests."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import spammer
from database import Database


class DeliveryTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        await self.db.init_db(1)
        await self.db.save_account(1, 1)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def make_campaign(self, minimum, maximum=None):
        cid = await self.db.create_campaign("timing", 1, minimum, maximum or minimum)
        await self.db.add_groups(cid, ["group_A", "group_B"])
        await self.db.add_message(cid, "post", "text")
        await self.db.start_campaign_fresh(cid)
        return cid

    async def run_sender(self, cid, clock, count=6, fail_first=False):
        events = []
        async def send(**kwargs):
            started = clock[0]
            clock[0] += 2  # Simulate a nonzero network request duration.
            events.append({"group": kwargs["chat_id"], "start": started, "finish": clock[0]})
            if fail_first and len(events) == 1:
                raise RuntimeError("simulated send failure")
        async def sleep(seconds):
            if len(events) >= count:
                raise asyncio.CancelledError
            clock[0] += seconds
        lock = asyncio.Lock()
        manager = SimpleNamespace(lock=lambda _: lock,
            get_client=AsyncMock(return_value=SimpleNamespace(send_message=AsyncMock(side_effect=send))),
            failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", manager), \
                patch.object(spammer.time, "time", lambda: clock[0]), \
                patch.object(spammer.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await spammer.run_campaign_spam(cid)
        return events

    def assert_group_intervals(self, events, minimum):
        for group in ("group_A", "group_B"):
            attempts = [event for event in events if event["group"] == group]
            self.assertGreaterEqual(len(attempts), 2)
            for previous, current in zip(attempts, attempts[1:]):
                self.assertGreaterEqual(current["start"] - previous["finish"], minimum)

    async def test_group_interval_is_counted_after_request_finishes(self):
        for interval in (7, 10, 30, 1800):
            with self.subTest(interval=interval):
                cid = await self.make_campaign(interval)
                events = await self.run_sender(cid, [1000.0])
                self.assert_group_intervals(events, interval)

    async def test_range_never_undercuts_group_minimum(self):
        for chosen in (25, 50):
            cid = await self.make_campaign(25, 50)
            with patch("database.random.randint", return_value=chosen):
                events = await self.run_sender(cid, [1000.0])
            self.assert_group_intervals(events, chosen)

    async def test_different_groups_use_separate_short_gap_not_group_interval(self):
        cid = await self.make_campaign(1800)
        events = await self.run_sender(cid, [1000.0], count=2)
        self.assertNotEqual(events[0]["group"], events[1]["group"])
        self.assertEqual(events[1]["start"] - events[0]["finish"], 10)

    async def test_error_keeps_both_group_interval_and_inter_group_gap(self):
        cid = await self.make_campaign(30)
        events = await self.run_sender(cid, [1000.0], fail_first=True)
        self.assert_group_intervals(events, 30)
        self.assertEqual(events[1]["start"] - events[0]["finish"], 10)

    async def test_worker_restart_does_not_bypass_group_interval(self):
        cid = await self.make_campaign(30)
        clock = [1000.0]
        first = await self.run_sender(cid, clock, count=2)
        second = await self.run_sender(cid, clock, count=2)
        self.assert_group_intervals(first + second, 30)
