import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pyrogram.errors import SessionPasswordNeeded
import handlers


class QRAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = object()
        self.qr = SimpleNamespace(client=self.client, wait=AsyncMock(return_value=object()))
        self.message = SimpleNamespace(answer=AsyncMock(), delete=AsyncMock())
        self.state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())
        self.lock = asyncio.Lock()

    async def invoke(self, error=None):
        if error:
            self.qr.wait.side_effect = error
        with patch.object(handlers.accounts, "pending", {42: (self.client, None, None)}), \
                patch.object(handlers.accounts, "qr_tasks", {}), \
                patch.object(handlers.accounts, "lock", return_value=self.lock), \
                patch.object(handlers.accounts, "activate", AsyncMock()) as activate, \
                patch.object(handlers.accounts, "cancel", AsyncMock()) as cancel, \
                patch.object(handlers, "get_campaigns_list_keyboard", AsyncMock(return_value=None)):
            await handlers.wait_for_qr(self.qr, self.message, self.state, 42, 42)
        return activate, cancel

    async def test_success_activates_and_deletes_qr(self):
        activate, _ = await self.invoke()
        activate.assert_awaited_once_with(42, self.client)
        self.message.delete.assert_awaited_once()

    async def test_timeout_cancels(self):
        activate, cancel = await self.invoke(asyncio.TimeoutError())
        activate.assert_not_awaited()
        cancel.assert_awaited_once_with(42)

    async def test_two_factor_requests_password(self):
        activate, cancel = await self.invoke(SessionPasswordNeeded())
        activate.assert_not_awaited()
        cancel.assert_not_awaited()
        self.state.set_state.assert_awaited_once_with(handlers.AccountLogin.password)

    async def test_unconnected_user_cannot_create_campaign(self):
        with patch.object(handlers.accounts, "is_connected", AsyncMock(return_value=False)):
            keyboard = await handlers.get_campaigns_list_keyboard(42, False)
        buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("account_connect", buttons)
        self.assertNotIn("create_campaign", buttons)

    async def test_connect_sends_qr_and_same_phone_button(self):
        qr = SimpleNamespace(url="tg://login?token=test")
        callback_message = SimpleNamespace(chat=SimpleNamespace(type="private"), answer_photo=AsyncMock(return_value=self.message))
        callback = SimpleNamespace(message=callback_message, answer=AsyncMock())
        tasks = {}
        with patch.object(handlers.accounts, "clients", {}), \
                patch.object(handlers.accounts, "qr_tasks", tasks), \
                patch.object(handlers.accounts, "lock", return_value=self.lock), \
                patch.object(handlers.accounts, "begin_qr", AsyncMock(return_value=qr)), \
                patch.object(handlers, "wait_for_qr", AsyncMock()):
            await handlers.account_connect(callback, self.state, 42, 42)
            await tasks[42]
        keyboard = callback_message.answer_photo.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].url, qr.url)
