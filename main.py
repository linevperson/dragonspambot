import asyncio
import logging
import os
import sys
from importlib import metadata
from pathlib import Path
from dotenv import load_dotenv
import pyrogram

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from database import db
from handlers import router
from accounts import accounts

# 1. Отключаем вывод ошибок от asyncio (убирает Task exception)
logging.getLogger("asyncio").get_child_handler = lambda *args: None
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

# 2. Отключаем мусор от Pyrogram (убирает Peer id invalid)
logging.getLogger("pyrogram").setLevel(logging.CRITICAL)
logging.getLogger("pyrogram.session.session").setLevel(logging.CRITICAL)
logging.getLogger("pyrogram.connection.connection").setLevel(logging.CRITICAL)

# 3. Настраиваем основной формат логов, чтобы видеть только работу спаммера
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


async def main():
    """Main entry point - initialize and run both bots."""
    try:
        kurigram = metadata.distribution("kurigram")
        expected_module = Path(kurigram.locate_file("pyrogram/__init__.py")).resolve()
        if Path(pyrogram.__file__).resolve() != expected_module:
            raise RuntimeError("Wrong Telegram library imported")
    except (metadata.PackageNotFoundError, RuntimeError):
        logger.error("Kurigram не найден или загружен другой пакет. Запустите .qr-venv/bin/python main.py")
        return
    # Load environment variables
    load_dotenv()
    
    bot_token = os.getenv("BOT_TOKEN")
    admin_id = os.getenv("ADMIN_ID")
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    
    # Validate configuration
    if not all([bot_token, admin_id, api_id, api_hash]):
        logger.error("Missing required environment variables!")
        logger.error("Please configure .env file with: BOT_TOKEN, ADMIN_ID, API_ID, API_HASH")
        return
    
    try:
        admin_id = int(admin_id)
        api_id = int(api_id)
    except ValueError:
        logger.error("ADMIN_ID and API_ID must be integers!")
        return
    
    logger.info("🚀 Starting Spam Bot...")
    
    # Initialize database
    logger.info("Initializing database...")
    await db.init_db(admin_id)
    if await accounts.register_existing_admin(admin_id):
        logger.info("Existing administrator session linked")
    
    # Initialize aiogram bot (admin panel)
    logger.info("Initializing admin bot...")
    bot = Bot(
        token=bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    
    # Check access on every update so revocations take effect immediately.
    from aiogram import BaseMiddleware
    from aiogram.types import Update
    from typing import Callable, Dict, Any, Awaitable
    
    class AdminMiddleware(BaseMiddleware):
        """Allow the owner and users granted access by the owner."""
        
        def __init__(self, admin_id: int):
            self.admin_id = admin_id
            super().__init__()
        
        async def __call__(
            self,
            handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
            event: Update,
            data: Dict[str, Any]
        ) -> Any:
            # Get user from event
            user = None
            if event.message:
                user = event.message.from_user
            elif event.callback_query:
                user = event.callback_query.from_user
            
            if user and (user.id == self.admin_id or await db.is_authorized_user(user.id)):
                if event.callback_query and event.callback_query.data:
                    callback_data = event.callback_query.data
                    campaign_actions = {
                        "campaign", "delete_campaign", "add_message", "list_messages",
                        "groups_menu", "add_groups", "choose_groups", "show_groups", "clear_groups",
                        "config_delay", "delay_seconds", "delay_minutes",
                        "start_campaign", "stop_campaign",
                    }
                    action, separator, campaign_id = callback_data.partition(":")
                    if (action == "create_campaign" or action in campaign_actions) and not await accounts.is_connected(user.id):
                        await event.callback_query.answer(
                            "Сначала подключите свой Telegram-аккаунт", show_alert=True
                        )
                        return
                    if action in campaign_actions:
                        if (not separator or not campaign_id.isdecimal() or
                                not await db.campaign_belongs_to(int(campaign_id), user.id)):
                            await event.callback_query.answer(
                                "Эта кампания вам недоступна", show_alert=True
                            )
                            return
                data["owner_id"] = self.admin_id
                data["user_id"] = user.id
                data["is_owner"] = user.id == self.admin_id
                return await handler(event, data)
            else:
                # Ignore non-admin users
                if event.message:
                    await event.message.answer(
                        "❌ Access denied. This bot is for admin only."
                    )
                elif event.callback_query:
                    await event.callback_query.answer(
                        "❌ Access denied",
                        show_alert=True
                    )
                return
    
    # Register admin middleware
    dp.update.middleware(AdminMiddleware(admin_id))
    
    # Register handlers
    dp.include_router(router)
    
    # Start admin bot
    logger.info("✅ Admin bot started")
    logger.info("=" * 50)
    logger.info("🤖 Spam Bot is running!")
    logger.info(f"👤 Admin ID: {admin_id}")
    logger.info("=" * 50)
    
    # Start campaign monitor in background
    logger.info("Starting campaign monitor...")
    from spammer import campaign_monitor
    monitor_task = asyncio.create_task(campaign_monitor(bot))
    
    try:
        # Run bot
        await dp.start_polling(bot)
    finally:
        # Cleanup
        logger.info("Shutting down...")
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        from spammer import campaign_tasks
        for task in campaign_tasks.values():
            task.cancel()
        await asyncio.gather(*campaign_tasks.values(), return_exceptions=True)
        await accounts.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
