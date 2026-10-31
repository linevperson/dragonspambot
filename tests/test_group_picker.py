import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
from spammer import normalize_group_destination


class GroupPickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state_data = {"campaign_id": 5, "group_select_request_id": 123}
        self.state = SimpleNamespace(get_data=AsyncMock(return_value=self.state_data), clear=AsyncMock())
        self.message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            chat_shared=SimpleNamespace(request_id=123, chat_id=-100123),
            answer=AsyncMock(),
        )
        self.lock = asyncio.Lock()

    async def test_picker_requests_group_without_requiring_bot_membership(self):
        message = SimpleNamespace(chat=SimpleNamespace(type="private"), answer=AsyncMock())
        callback = SimpleNamespace(data="choose_groups:5", message=message, answer=AsyncMock())
        state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
        await handlers.choose_groups_start(callback, state)
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        request = keyboard.keyboard[0][0].request_chat
        self.assertFalse(request.chat_is_channel)
        self.assertIsNone(request.bot_is_member)
        self.assertEqual(state.update_data.await_args.kwargs["group_select_request_id"], request.request_id)

    async def test_selected_group_is_verified_and_saved_as_numeric_id(self):
        chat = SimpleNamespace(id=-100123, title="A & B", username="mygroup", type=SimpleNamespace(value="supergroup"))
        async def dialogs(from_archive=False):
            if not from_archive:
                yield SimpleNamespace(chat=chat)
        client = SimpleNamespace(get_dialogs=dialogs, storage=SimpleNamespace(save=AsyncMock()))
        with patch.object(handlers.accounts, "lock", return_value=self.lock), \
                patch.object(handlers.accounts, "get_client", AsyncMock(return_value=client)), \
                patch.object(handlers.db, "campaign_belongs_to", AsyncMock(return_value=True)), \
                patch.object(handlers.db, "get_campaign_groups", AsyncMock(return_value=[])), \
                patch.object(handlers.db, "add_group_for_owner", AsyncMock(return_value=True)) as add:
            await handlers.choose_groups_save(self.message, self.state, user_id=42, is_owner=True)
        add.assert_awaited_once_with(5, 42, "-100123")
        client.storage.save.assert_awaited_once()
        self.assertIn("A &amp; B", self.message.answer.await_args.args[0])

    async def test_foreign_campaign_is_not_modified(self):
        with patch.object(handlers.accounts, "lock", return_value=self.lock), \
                patch.object(handlers.db, "campaign_belongs_to", AsyncMock(return_value=False)), \
                patch.object(handlers.db, "add_group_for_owner", AsyncMock()) as add:
            await handlers.choose_groups_save(self.message, self.state, user_id=42, is_owner=True)
        add.assert_not_awaited()
        self.state.clear.assert_awaited_once()

    async def test_old_selection_cannot_be_used_in_another_campaign(self):
        self.message.chat_shared.request_id = 999
        with patch.object(handlers.db, "add_group_for_owner", AsyncMock()) as add:
            await handlers.choose_groups_save(self.message, self.state, user_id=42, is_owner=True)
        add.assert_not_awaited()

    async def test_unavailable_group_is_not_saved(self):
        async def dialogs(from_archive=False):
            if False:
                yield None
        client = SimpleNamespace(get_dialogs=dialogs)
        with patch.object(handlers.accounts, "lock", return_value=self.lock), \
                patch.object(handlers.accounts, "get_client", AsyncMock(return_value=client)), \
                patch.object(handlers.db, "campaign_belongs_to", AsyncMock(return_value=True)), \
                patch.object(handlers.db, "add_group_for_owner", AsyncMock()) as add:
            await handlers.choose_groups_save(self.message, self.state, user_id=42, is_owner=True)
        add.assert_not_awaited()

    def test_numeric_group_id_is_passed_as_int(self):
        self.assertEqual(normalize_group_destination("-100123"), -100123)
        self.assertEqual(normalize_group_destination("https://t.me/groupname"), "groupname")
