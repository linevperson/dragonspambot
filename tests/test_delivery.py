import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pyrogram.errors import PeerIdInvalid
from spammer import ensure_group_peer, send_campaign_message


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_peer_is_loaded_and_persisted(self):
        async def dialogs(from_archive=False):
            if not from_archive:
                yield SimpleNamespace(chat=SimpleNamespace(id=-100123))
        storage = SimpleNamespace(get_peer_by_id=AsyncMock(side_effect=KeyError()), save=AsyncMock())
        await ensure_group_peer(SimpleNamespace(storage=storage, get_dialogs=dialogs), -100123)
        storage.save.assert_awaited_once()

    async def test_missing_group_fails_without_sending(self):
        async def dialogs(from_archive=False):
            if False:
                yield None
        storage = SimpleNamespace(get_peer_by_id=AsyncMock(side_effect=KeyError()), save=AsyncMock())
        with self.assertRaises(PeerIdInvalid):
            await ensure_group_peer(SimpleNamespace(storage=storage, get_dialogs=dialogs), -100123)

    async def test_photo_is_downloaded_then_uploaded_by_sender(self):
        async def download(file_id, destination):
            destination.write(b"photo-data")
        bot = SimpleNamespace(download=AsyncMock(side_effect=download))
        client = SimpleNamespace(send_photo=AsyncMock())
        await send_campaign_message(client, {"media_type": "photo", "media_id": "bot-file-id", "text": "caption"}, -100123, bot)
        bot.download.assert_awaited_once()
        kwargs = client.send_photo.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -100123)
        self.assertEqual(kwargs["photo"].read(), b"photo-data")
        self.assertEqual(kwargs["caption"], "caption")

    async def test_stopped_during_download_does_not_send(self):
        async def download(file_id, destination):
            destination.write(b"photo-data")
        bot = SimpleNamespace(download=AsyncMock(side_effect=download))
        client = SimpleNamespace(send_photo=AsyncMock())
        sent = await send_campaign_message(
            client, {"media_type": "photo", "media_id": "bot-file-id", "text": "caption"},
            -100123, bot, AsyncMock(return_value=False),
        )
        self.assertFalse(sent)
        client.send_photo.assert_not_awaited()
