import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pyrogram.errors import SlowmodeWait, ChatSendRoundvideosForbidden, ChatWriteForbidden, FloodWait, PeerFlood

import handlers
import spammer
from database import Database


class GroupFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spammer.campaign_storage_blocked.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.db = Database(str(self.path))
        await self.db.init_db(42)
        await self.db.save_account(42, 42)
        self.cid = await self.db.create_campaign("A <B>", 42, 30, 30)
        await self.db.add_groups(self.cid, ["group_A", "group_B"])
        self.groups = await self.db.get_campaign_groups(self.cid)
        await self.db.cache_group_title(self.cid, self.groups[0]["id"], "Chat <A> & B")
        self.circle = await self.db.add_message(self.cid, None, "video_note", "circle", media_metadata={"duration": 5, "length": 240})
        self.text = await self.db.add_message(self.cid, "text", "text")
        await self.db.start_campaign_fresh(self.cid)
        self.clock = [1000.0]
        self.bot = SimpleNamespace(send_message=AsyncMock(), download=AsyncMock(side_effect=self.download))
        self.events = []
        self.manager = SimpleNamespace(lock=lambda _: asyncio.Lock(), get_client=AsyncMock(), failed_flood_writes=set())

    async def asyncTearDown(self):
        spammer.campaign_storage_blocked.clear()
        self.tmp.cleanup()

    async def download(self, file_id, destination):
        destination.write(file_id.encode())

    async def run_worker(self, error_for=lambda group, kind: None, count=6, horizon=1200, notify_error=False):
        async def send(kind, kwargs):
            self.events.append((kwargs["chat_id"], kind, self.clock[0]))
            exc = error_for(kwargs["chat_id"], kind)
            if exc:
                raise exc
        async def text_send(**kwargs):
            return await send("text", kwargs)
        async def circle_send(**kwargs):
            return await send("video_note", kwargs)
        client = SimpleNamespace(send_message=AsyncMock(side_effect=text_send), send_video_note=AsyncMock(side_effect=circle_send))
        self.manager.get_client.return_value = client
        if notify_error:
            self.bot.send_message.side_effect = RuntimeError("notifications unavailable")
        async def sleep(seconds):
            if len(self.events) >= count or self.clock[0] >= horizon:
                raise asyncio.CancelledError
            self.clock[0] += seconds
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", self.manager), \
                patch.object(spammer.time, "time", lambda: self.clock[0]), patch.object(spammer.asyncio, "sleep", sleep):
            try:
                await spammer.run_campaign_spam(self.cid, self.bot)
            except asyncio.CancelledError:
                pass

    async def test_slowmode_waits_without_blocking_other_groups_and_survives_fresh_start(self):
        await self.run_worker(lambda group, kind: SlowmodeWait(300) if group == "group_A" else None)
        self.assertEqual(len([e for e in self.events if e[0] == "group_A"]), 1)
        self.assertGreaterEqual(len([e for e in self.events if e[0] == "group_B"]), 3)
        group = await self.db.get_group(self.cid, self.groups[0]["id"])
        self.assertEqual(group["retry_after"], 1300)
        self.assertIsNone(group["last_message_id"])
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])
        await self.db.init_db(42)
        await self.db.start_campaign_fresh(self.cid)
        self.assertFalse(await self.db.reserve_group_attempt(self.cid, group["id"], 1299))
        self.assertTrue(await self.db.reserve_group_attempt(self.cid, group["id"], 1300))

    async def test_short_slowmode_does_not_shorten_configured_interval(self):
        await self.db.update_campaign_delay(self.cid, 300, 300)
        await self.run_worker(lambda group, kind: SlowmodeWait(10) if group == "group_A" else None, count=2)
        group = await self.db.get_group(self.cid, self.groups[0]["id"])
        self.assertEqual(max(group["retry_after"], group["next_attempt_at"]), 1300)

    async def test_forbidden_circle_skips_only_that_group_format(self):
        await self.run_worker(lambda group, kind: ChatSendRoundvideosForbidden() if group == "group_A" and kind == "video_note" else None)
        a = [kind for group, kind, _ in self.events if group == "group_A"]
        b = [kind for group, kind, _ in self.events if group == "group_B"]
        self.assertEqual(a, ["video_note", "text", "text"])
        self.assertEqual(b, ["text", "video_note", "text"])
        group = await self.db.get_group(self.cid, self.groups[0]["id"])
        self.assertTrue(group["is_enabled"])
        self.assertEqual(json.loads(group["blocked_media_types"]), ["video_note"])
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])

    async def test_all_formats_forbidden_pauses_only_one_group(self):
        await self.db.replace_message(self.cid, self.text, None, "video_note", "circle2", "[]", {"length": 240})
        await self.run_worker(lambda group, kind: ChatSendRoundvideosForbidden() if group == "group_A" else None, count=4)
        self.assertFalse((await self.db.get_group(self.cid, self.groups[0]["id"]))["is_enabled"])
        self.assertTrue((await self.db.get_group(self.cid, self.groups[1]["id"]))["is_enabled"])
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])

    async def test_no_write_access_pauses_one_group_and_notifies_owner_with_escaped_title(self):
        await self.run_worker(lambda group, kind: ChatWriteForbidden() if group == "group_A" else None, count=4)
        self.assertEqual(len([e for e in self.events if e[0] == "group_A"]), 1)
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])
        self.bot.send_message.assert_awaited_once()
        call = self.bot.send_message.await_args
        self.assertEqual(call.args[0], 42)
        self.assertIn("Chat &lt;A&gt; &amp; B", call.args[1])
        self.assertIn("Нет разрешения писать", call.args[1])

    async def test_repeated_error_is_throttled_and_notification_failure_does_not_stop_sending(self):
        await self.run_worker(lambda group, kind: RuntimeError() if group == "group_A" else None, notify_error=True)
        self.assertEqual(len([e for e in self.events if e[0] == "group_B"]), 3)
        self.bot.send_message.assert_awaited_once()
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])

    async def test_notification_deduplication_is_persistent_and_changes_of_reason_notify(self):
        group = self.groups[0]["id"]
        self.assertTrue(await self.db.claim_group_notification(self.cid, group, "slowmode", 1000))
        await self.db.init_db(42)
        self.assertFalse(await self.db.claim_group_notification(self.cid, group, "slowmode", 1001))
        self.assertTrue(await self.db.claim_group_notification(self.cid, group, "slowmode", 1900))
        self.assertTrue(await self.db.claim_group_notification(self.cid, group, "forbidden", 1901))
        self.assertFalse(await self.db.claim_group_notification(self.cid, group, "slowmode", 1902))

    async def test_manual_enable_rechecks_media_but_does_not_reset_telegram_wait(self):
        group = self.groups[0]["id"]
        await self.db.record_group_failure(self.cid, group, "restriction", retry_after=1300, blocked_types={"video_note"}, pause=True)
        await self.db.set_group_enabled(self.cid, group, 42, True)
        current = await self.db.get_group(self.cid, group)
        self.assertEqual(json.loads(current["blocked_media_types"]), [])
        self.assertIsNone(current["pause_reason"])
        self.assertEqual(current["retry_after"], 1300)

    async def test_uncertain_network_result_pauses_only_target_instead_of_duplicate_retry(self):
        await self.run_worker(lambda group, kind: TimeoutError() if group == "group_A" else None, count=4)
        self.assertEqual(len([e for e in self.events if e[0] == "group_A"]), 1)
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])
        self.assertIn("результат отправки не подтверждён", self.bot.send_message.await_args.args[1])

    async def test_cursor_write_failure_stops_campaign_before_duplicate(self):
        with patch.object(self.db, "record_group_message", AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            await self.run_worker()
        self.assertEqual(len(self.events), 1)
        self.assertIn(self.cid, spammer.campaign_storage_blocked)
        self.assertEqual((await self.db.get_campaign(self.cid))["stop_reason"], "storage")
        await self.run_worker()
        self.assertEqual(len(self.events), 1)

    async def test_schedule_write_failure_is_not_swallowed_as_group_error(self):
        with patch.object(self.db, "finish_group_attempt", AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            await self.run_worker()
        self.assertEqual(len(self.events), 1)
        self.assertIn(self.cid, spammer.campaign_storage_blocked)

    async def test_in_memory_guard_works_even_when_stop_cannot_be_saved(self):
        with patch.object(self.db, "record_group_message", AsyncMock(side_effect=sqlite3.OperationalError("locked"))), \
                patch.object(self.db, "set_campaign_running", AsyncMock(side_effect=sqlite3.OperationalError("locked"))):
            await self.run_worker()
        self.assertIn(self.cid, spammer.campaign_storage_blocked)
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])
        await self.run_worker()
        self.assertEqual(len(self.events), 1)

    async def test_account_restriction_is_not_misclassified_as_local_error(self):
        await self.run_worker(lambda group, kind: PeerFlood())
        self.assertFalse((await self.db.get_campaign(self.cid))["is_running"])
        self.assertIn(42, self.manager.failed_flood_writes)
        self.assertEqual(len(self.events), 1)

    async def test_floodwait_stops_account_and_notifies_owner(self):
        await self.run_worker(lambda group, kind: FloodWait(300))
        self.assertFalse((await self.db.get_campaign(self.cid))["is_running"])
        self.assertIn("весь отправляющий аккаунт", self.bot.send_message.await_args.args[1])

    async def test_group_details_and_campaign_eta_include_telegram_cooldown(self):
        group_id = self.groups[0]["id"]
        await self.db.record_group_failure(self.cid, group_id, "Медленный режим", retry_after=1300)
        await self.db.set_group_enabled(self.cid, self.groups[1]["id"], 42, False)
        # Fresh start resets the chosen interval, not Telegram's restriction.
        await self.db.set_campaign_running(self.cid, False)
        await self.db.start_campaign_fresh(self.cid)
        self.assertEqual((await self.db.get_campaign_status(self.cid))["next_attempt_at"], 1300)
        callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
        with patch.object(handlers, "db", self.db), patch.object(handlers.time, "time", return_value=1000):
            await handlers.show_group_details(callback, self.cid, group_id)
        text = callback.message.edit_text.await_args.args[0]
        self.assertIn("Медленный режим", text)
        self.assertNotIn("сейчас / в очереди", text)

    async def test_storage_safety_guard_rejects_manual_start(self):
        spammer.campaign_storage_blocked.add(self.cid)
        callback = SimpleNamespace(data=f"start_campaign:{self.cid}", answer=AsyncMock())
        await handlers.start_campaign(callback, 42, 42)
        self.assertIn("сбоя БД", callback.answer.await_args.args[0])

    async def test_notification_ledger_failure_alone_does_not_stop_campaign(self):
        with patch.object(self.db, "claim_group_notification", AsyncMock(side_effect=sqlite3.OperationalError("notification ledger locked"))):
            await self.run_worker(lambda group, kind: RuntimeError() if group == "group_A" else None)
        self.assertEqual(len([e for e in self.events if e[0] == "group_B"]), 3)
        self.assertTrue((await self.db.get_campaign(self.cid))["is_running"])
        self.assertNotIn(self.cid, spammer.campaign_storage_blocked)
