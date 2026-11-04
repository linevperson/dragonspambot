import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
import spammer
from database import Database
from campaign_status import format_campaign_status, format_attempt_time


class StatusAndGroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        await self.db.init_db(1)
        await self.db.save_account(1, 1)
        self.campaign = await self.db.create_campaign("test", 1, 1800, 1800)
        await self.db.add_message(self.campaign, "hello", "text")
        await self.db.add_groups(self.campaign, ["@one", "@two"])
        self.one, self.two = [g["id"] for g in await self.db.get_campaign_groups(self.campaign)]
        await self.db.start_campaign_fresh(self.campaign)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def status(self, **kwargs):
        return format_campaign_status(await self.db.get_campaign_status(self.campaign), now=1001, **kwargs)

    async def test_status_distinguishes_queued_sending_and_waiting(self):
        self.assertIn("Ожидает отправки", await self.status())
        self.assertIn("Отправляет", await self.status(sending=True))
        for group_id in (self.one, self.two):
            await self.db.reserve_group_attempt(self.campaign, group_id, 1000)
        snapshot = await self.db.get_campaign_status(self.campaign)
        self.assertEqual(snapshot["last_attempt_at"], 1000)
        self.assertIn("Ожидает следующую отправку", await self.status())
        self.assertIn(format_attempt_time(2800), await self.status())

    async def test_last_attempt_survives_restart_of_campaign_and_group_deletion(self):
        await self.db.reserve_group_attempt(self.campaign, self.one, 1000)
        await self.db.set_campaign_running(self.campaign, False)
        await self.db.start_campaign_fresh(self.campaign)
        await self.db.delete_group_for_owner(self.campaign, self.one, 1)
        snapshot = await self.db.get_campaign_status(self.campaign)
        self.assertEqual(snapshot["last_attempt_at"], 1000)
        self.assertEqual(snapshot["next_attempt_at"], 0)

    async def test_stopped_campaign_has_no_next_attempt(self):
        await self.db.set_campaign_running(self.campaign, False)
        self.assertIn("Ближайшая попытка: —", await self.status())

    async def test_no_posts_and_no_enabled_groups_are_explained(self):
        await self.db.clear_campaign_messages(self.campaign)
        self.assertIn("Нет доступных постов", await self.status())
        await self.db.add_message(self.campaign, "hello", "text")
        for group_id in (self.one, self.two):
            await self.db.set_group_enabled(self.campaign, group_id, 1, False)
        self.assertIn("Все группы выключены", await self.status())
        await self.db.clear_campaign_groups(self.campaign)
        self.assertIn("Нет групп", await self.status())

    async def test_flood_reason_survives_expiry_and_bot_restart(self):
        with patch("database.time.time", return_value=1000):
            await self.db.stop_account_for_flood(1, 60)
        self.assertIn("Остановлена ограничением Telegram", await self.status())
        await self.db.init_db(1)
        snapshot = await self.db.get_campaign_status(self.campaign)
        text = format_campaign_status(snapshot, now=1061)
        self.assertIn("можно запустить вручную", text)
        self.assertIn("Ближайшая попытка: —", text)

    async def test_restart_is_not_reported_as_manual_stop(self):
        await self.db.init_db(1)
        self.assertIn("после перезапуска бота", await self.status())

    async def test_disabled_group_cannot_be_reserved_and_survives_restart(self):
        await self.db.set_group_enabled(self.campaign, self.one, 1, False)
        self.assertFalse(await self.db.reserve_group_attempt(self.campaign, self.one, 1000))
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, self.two, 1000))
        await self.db.init_db(1)
        self.assertFalse((await self.db.get_group(self.campaign, self.one))["is_enabled"])
        self.assertEqual((await self.db.get_campaign_status(self.campaign))["enabled_group_count"], 1)
        self.assertEqual((await self.db.get_campaign_status(self.campaign))["next_attempt_at"], 2800)

    async def test_sending_flag_tracks_network_call_and_clears_on_cancellation(self):
        async def send_message(**kwargs):
            self.assertIn(self.campaign, spammer.campaign_sending)
            raise asyncio.CancelledError
        client = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
        accounts = SimpleNamespace(lock=lambda _: asyncio.Lock(), get_client=AsyncMock(return_value=client), failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", accounts):
            with self.assertRaises(asyncio.CancelledError):
                await spammer.run_campaign_spam(self.campaign)
        self.assertNotIn(self.campaign, spammer.campaign_sending)

    async def test_reenable_keeps_schedule(self):
        await self.db.reserve_group_attempt(self.campaign, self.one, 1000)
        await self.db.set_group_enabled(self.campaign, self.one, 1, False)
        await self.db.set_group_enabled(self.campaign, self.one, 1, True)
        self.assertFalse(await self.db.reserve_group_attempt(self.campaign, self.one, 1001))
        self.assertTrue(await self.db.reserve_group_attempt(self.campaign, self.one, 2800))

    async def test_foreign_group_is_not_changed(self):
        self.assertFalse(await self.db.set_group_enabled(self.campaign, self.one, 2, False))
        self.assertFalse(await self.db.delete_group_for_owner(self.campaign, self.one, 2))
        other = await self.db.create_campaign("other", 1)
        self.assertFalse(await self.db.set_group_enabled(other, self.one, 1, False))
        self.assertFalse(await self.db.delete_group_for_owner(other, self.one, 1))
        self.assertTrue((await self.db.get_group(self.campaign, self.one))["is_enabled"])

    async def test_disabled_group_is_not_sent_and_sending_flag_is_cleared(self):
        await self.db.set_group_enabled(self.campaign, self.one, 1, False)
        client = SimpleNamespace(send_message=AsyncMock())
        accounts = SimpleNamespace(lock=lambda _: asyncio.Lock(), get_client=AsyncMock(return_value=client), failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", accounts), \
                patch.object(spammer.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await spammer.run_campaign_spam(self.campaign)
        client.send_message.assert_awaited_once()
        self.assertEqual(client.send_message.await_args.kwargs["chat_id"], "two")
        self.assertNotIn(self.campaign, spammer.campaign_sending)

    async def test_disabling_during_media_download_prevents_upload(self):
        await self.db.clear_campaign_messages(self.campaign)
        await self.db.add_message(self.campaign, "caption", "photo", "file")
        await self.db.set_group_enabled(self.campaign, self.two, 1, False)
        async def download(file_id, destination):
            destination.write(b"photo")
            await self.db.set_group_enabled(self.campaign, self.one, 1, False)
        client = SimpleNamespace(send_photo=AsyncMock())
        accounts = SimpleNamespace(lock=lambda _: asyncio.Lock(), get_client=AsyncMock(return_value=client), failed_flood_writes=set())
        with patch.object(spammer, "db", self.db), patch.object(spammer, "accounts", accounts), \
                patch.object(spammer.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await spammer.run_campaign_spam(self.campaign, SimpleNamespace(download=AsyncMock(side_effect=download)))
        client.send_photo.assert_not_awaited()
        self.assertNotIn(self.campaign, spammer.campaign_sending)

    async def test_group_ui_requires_confirmation_and_keeps_other_groups(self):
        callback = SimpleNamespace(
            data=f"group_delete:{self.campaign}:{self.one}", answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        with patch.object(handlers, "db", self.db):
            await handlers.group_action(callback, 1)
            self.assertIsNotNone(await self.db.get_group(self.campaign, self.one))
            callback.data = f"group_confirm:{self.campaign}:{self.one}"
            await handlers.group_action(callback, 1)
        self.assertIsNone(await self.db.get_group(self.campaign, self.one))
        self.assertIsNotNone(await self.db.get_group(self.campaign, self.two))
        self.assertIsNotNone(await self.db.get_campaign(self.campaign))
