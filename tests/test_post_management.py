import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import MessageEntity
from pyrogram import enums

import handlers
import spammer
from database import Database
from post_content import serialize_entities, sender_entities


class MemoryState:
    def __init__(self, **data):
        self.data = data
        self.current = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def clear(self):
        self.data.clear()
        self.current = None

    async def set_state(self, value):
        self.current = value


class PostManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        await self.db.init_db(1)
        self.campaign = await self.db.create_campaign("test", 1, 1800, 1800)
        self.foreign = await self.db.create_campaign("foreign", 2, 60, 60)
        self.entities = serialize_entities([
            MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="1234567890123456789"),
            MessageEntity(type="bold", offset=3, length=4),
        ])
        self.state = MemoryState()
        self.callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1), data=f"list_messages:{self.campaign}",
            bot=SimpleNamespace(delete_message=AsyncMock()), answer=AsyncMock(),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=1), answer=AsyncMock(return_value=SimpleNamespace(message_id=101)),
                answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=102)),
                answer_video=AsyncMock(return_value=SimpleNamespace(message_id=103)),
                answer_video_note=AsyncMock(return_value=SimpleNamespace(message_id=104)),
                edit_reply_markup=AsyncMock(), edit_text=AsyncMock(),
            ),
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def add(self, kind="text", campaign=None):
        return await self.db.add_message(campaign or self.campaign, "😀 bold <x>", kind, "file", self.entities, {"duration": 15, "length": 240})

    async def test_migration_preserves_legacy_posts(self):
        path = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, campaign_id INTEGER, text TEXT, media_type TEXT, media_id TEXT)")
            conn.execute("INSERT INTO messages VALUES (7, 1, 'legacy', 'text', NULL)")
        legacy = Database(str(path))
        await legacy.init_db(1)
        await legacy.init_db(1)
        post = await legacy.get_message(7)
        self.assertEqual(post["text"], "legacy")
        self.assertIsNone(post["entities_json"])

    async def test_entities_survive_db_and_sender_conversion(self):
        post = await self.db.get_message(await self.add())
        entities = sender_entities(post["entities_json"])
        self.assertEqual(entities[0].custom_emoji_id, "1234567890123456789")
        self.assertEqual(entities[1].offset, 3)  # UTF-16 offset after a surrogate-pair emoji
        self.assertEqual(entities[0].type, enums.MessageEntityType.CUSTOM_EMOJI)
        client = SimpleNamespace(send_message=AsyncMock())
        await spammer.send_campaign_message(client, post, -100123)
        kwargs = client.send_message.await_args.kwargs
        self.assertEqual(kwargs["parse_mode"], enums.ParseMode.DISABLED)
        self.assertEqual(kwargs["entities"][0].custom_emoji_id, "1234567890123456789")
        self.assertIn("<x>", kwargs["text"])
        raw = await entities[0].write()
        self.assertEqual(raw.document_id, 1234567890123456789)
        self.assertEqual((raw.offset, raw.length), (0, 2))

    async def test_save_text_and_caption_keeps_custom_emoji(self):
        for kind in ("text", "photo", "video"):
            state = MemoryState(campaign_id=self.campaign)
            entities = [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="123")]
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=1), caption="😀" if kind != "text" else None,
                text="😀" if kind == "text" else None,
                photo=[SimpleNamespace(file_id="photo")] if kind == "photo" else None,
                video=SimpleNamespace(file_id="video") if kind == "video" else None,
                video_note=None, entities=entities if kind == "text" else None,
                caption_entities=entities if kind != "text" else None, answer=AsyncMock(),
            )
            with patch.object(handlers, "db", self.db), patch.object(handlers, "require_connected_account", AsyncMock(return_value=True)):
                await handlers.add_message_save(message, state)
            post = (await self.db.get_campaign_messages(self.campaign))[-1]
            self.assertEqual(json.loads(post["entities_json"])[0]["custom_emoji_id"], "123")
            self.assertEqual(post["media_type"], kind)

    async def test_caption_entities_and_circle_delivery(self):
        async def download(file_id, destination):
            destination.write(b"media")
        bot = SimpleNamespace(download=AsyncMock(side_effect=download))
        client = SimpleNamespace(send_photo=AsyncMock(), send_video=AsyncMock(), send_video_note=AsyncMock())
        for kind in ("photo", "video", "video_note"):
            post = await self.db.get_message(await self.add(kind))
            await spammer.send_campaign_message(client, post, -100123, bot)
        self.assertEqual(client.send_photo.await_args.kwargs["caption_entities"][0].custom_emoji_id, "1234567890123456789")
        self.assertEqual(client.send_video.await_args.kwargs["caption_entities"][1].offset, 3)
        kwargs = client.send_video_note.await_args.kwargs
        self.assertEqual((kwargs["duration"], kwargs["length"]), (15, 240))
        self.assertEqual(kwargs["video_note"].read(), b"media")
        self.assertNotIn("caption", kwargs)

    async def test_cards_navigation_preview_and_back(self):
        await self.add()
        await self.add("video_note")
        with patch.object(handlers, "db", self.db):
            await handlers.list_messages(self.callback, self.state)
            kwargs = self.callback.message.answer.await_args.kwargs
            self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][1].text, "1 / 2")
            self.assertIsNone(kwargs["parse_mode"])
            self.callback.data = f"post_card:{self.campaign}:1"
            await handlers.post_card_action(self.callback, self.state)
            self.callback.bot.delete_message.assert_awaited_with(1, 101)
            self.callback.message.answer_video_note.assert_awaited_once()
            self.callback.data = f"post_back:{self.campaign}"
            await handlers.post_card_action(self.callback, self.state)
            self.assertEqual(self.state.data, {})

    async def test_delete_requires_confirmation_and_handles_last_post(self):
        post_id = await self.add("photo")
        with patch.object(handlers, "db", self.db):
            await handlers.list_messages(self.callback, self.state)
            self.callback.data = f"post_delete:{self.campaign}:{post_id}:0"
            await handlers.post_card_action(self.callback, self.state)
            self.assertIsNotNone(await self.db.get_message(post_id))
            self.callback.message.edit_reply_markup.assert_awaited_once()
            self.callback.data = f"post_confirm:{self.campaign}:{post_id}:0"
            await handlers.post_card_action(self.callback, self.state)
            self.assertIsNone(await self.db.get_message(post_id))
            self.assertIsNotNone(await self.db.get_campaign(self.campaign))

    async def test_foreign_post_cannot_be_edited_or_deleted(self):
        post_id = await self.add(campaign=self.foreign)
        with patch.object(handlers, "db", self.db):
            for action in ("post_edit", "post_confirm"):
                self.callback.data = f"{action}:{self.campaign}:{post_id}:0"
                await handlers.post_card_action(self.callback, self.state)
        self.assertIsNotNone(await self.db.get_message(post_id))
        self.assertFalse(await self.db.replace_message(self.campaign, post_id, "bad", "text", None, None, {}))
        self.assertFalse(await self.db.delete_campaign_message(self.campaign, post_id))

    async def test_edit_saves_circle_in_same_post(self):
        post_id = await self.add()
        self.callback.data = f"post_edit:{self.campaign}:{post_id}"
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1), caption=None, text=None, photo=None, video=None,
            video_note=SimpleNamespace(file_id="circle", duration=12, length=360),
            entities=None, caption_entities=None, answer=AsyncMock(),
        )
        with patch.object(handlers, "db", self.db), patch.object(handlers, "require_connected_account", AsyncMock(return_value=True)):
            await handlers.post_card_action(self.callback, self.state)
            await handlers.add_message_save(message, self.state)
        posts = await self.db.get_campaign_messages(self.campaign)
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["id"], posts[0]["media_type"]), (post_id, "video_note"))
        self.assertEqual(json.loads(posts[0]["media_metadata"])["length"], 360)

    async def test_group_titles_are_resolved_cached_and_escaped(self):
        await self.db.add_groups(self.campaign, ["@groupname"])
        self.callback.data = f"show_groups:{self.campaign}"
        client = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(title="A <B> & C")))
        with patch.object(handlers, "db", self.db), \
                patch.object(handlers, "flood_wait_remaining", AsyncMock(return_value=0)), \
                patch.object(handlers.accounts, "lock", return_value=asyncio.Lock()), \
                patch.object(handlers.accounts, "get_client", AsyncMock(return_value=client)):
            await handlers.show_groups(self.callback, 1)
            self.assertIn("A &lt;B&gt; &amp; C", self.callback.message.edit_text.await_args.args[0])
            await handlers.show_groups(self.callback, 1)
        client.get_chat.assert_awaited_once_with("groupname")
        self.assertEqual((await self.db.get_campaign_groups(self.campaign))[0]["group_title"], "A <B> & C")

    async def test_unavailable_group_title_falls_back_without_removing_group(self):
        await self.db.add_groups(self.campaign, ["@unavailable"])
        self.callback.data = f"show_groups:{self.campaign}"
        client = SimpleNamespace(get_chat=AsyncMock(side_effect=ValueError("unavailable")))
        with patch.object(handlers, "db", self.db), \
                patch.object(handlers, "flood_wait_remaining", AsyncMock(return_value=0)), \
                patch.object(handlers.accounts, "lock", return_value=asyncio.Lock()), \
                patch.object(handlers.accounts, "get_client", AsyncMock(return_value=client)):
            await handlers.show_groups(self.callback, 1)
        self.assertIn("@unavailable", self.callback.message.edit_text.await_args.args[0])
        self.assertEqual(len(await self.db.get_campaign_groups(self.campaign)), 1)
