import asyncio
import logging
import time
import json
import sqlite3
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict

from pyrogram import Client, enums
from pyrogram.errors import FloodWait, ChatWriteForbidden, UserBannedInChannel, ChannelPrivate, ChatAdminRequired, SlowmodeWait, PeerFlood, UserRestricted, Unauthorized
from pyrogram.errors.exceptions.bad_request_400 import PeerIdInvalid, UsernameInvalid

from database import db
from accounts import accounts
from post_content import sender_entities
from delivery_errors import media_restriction, DeliveryUncertainError
from storage_recovery import CampaignStorage

logger = logging.getLogger(__name__)

# Dictionary to track running campaign tasks
campaign_tasks: Dict[int, asyncio.Task] = {}
campaign_sending: Dict[int, int] = {}
campaign_storage_waiting = set()
campaign_storage_sessions = {}
account_flood_pending = set()


async def send_notice(admin_bot, owner_id, text):
    if not admin_bot or not hasattr(admin_bot, "send_message"):
        return
    try:
        await asyncio.wait_for(admin_bot.send_message(owner_id, text, parse_mode="HTML"), timeout=10)
    except Exception as exc:
        logger.warning("Could not deliver owner notification: %s", type(exc).__name__)


async def notify_group_error(admin_bot, campaign, group, reason, action, key):
    if not admin_bot or not hasattr(admin_bot, "send_message"):
        return
    try:
        claimed = await db.claim_group_notification(campaign["id"], group["id"], key, time.time())
    except sqlite3.Error as exc:
        # A notification ledger is not the delivery cursor/schedule. Do not halt
        # healthy groups solely because this optional notification cannot be claimed.
        logger.warning("Could not reserve error notification: %s", type(exc).__name__)
        return
    if not claimed:
        return
    title = group.get("group_title") or group["group_link"]
    await send_notice(admin_bot, campaign["owner_id"],
        f"⚠️ Не удалось отправить пост\nКампания: <b>{escape(campaign['name'][:200])}</b>\n"
        f"Группа: <b>{escape(title[:180])}</b>\nПричина: {escape(reason)}\n\n{escape(action)}")


async def handle_group_error(exc, campaign, group, admin_bot, storage=None):
    storage = storage or db
    cid, gid = campaign["id"], group["id"]
    restriction = media_restriction(exc)
    if isinstance(exc, SlowmodeWait):
        seconds = max(1, int(exc.value))
        reason = f"Telegram включил медленный режим: ждать {seconds} сек."
        await storage.record_group_failure(cid, gid, reason, retry_after=time.time() + seconds)
        action = "Пост сохранён в очереди. Эта группа ждёт не меньше срока Telegram и интервала кампании; остальные продолжают рассылку."
    elif restriction:
        blocked, reason = restriction
        await storage.record_group_failure(cid, gid, reason, blocked_types=blocked)
        if await storage.get_next_group_message(cid, gid):
            action = "Запрещённый формат пропущен только в этой группе. Следующая попытка — с обычным интервалом. Остальные группы продолжают рассылку."
        else:
            reason += "; нет подходящих вариантов постов"
            await storage.record_group_failure(cid, gid, reason, pause=True)
            action = "Приостановлена только эта группа. Добавьте подходящий пост или измените права и включите группу в её карточке."
    elif isinstance(exc, (ChatWriteForbidden, UserBannedInChannel, ChannelPrivate, ChatAdminRequired, PeerIdInvalid, UsernameInvalid)):
        reason = {
            ChatWriteForbidden: "Нет разрешения писать в группу",
            UserBannedInChannel: "Отправляющий аккаунт заблокирован в группе",
            ChannelPrivate: "Группа недоступна отправляющему аккаунту",
            ChatAdminRequired: "Для отправки нужны права администратора группы",
            PeerIdInvalid: "Не удалось найти группу среди доступных отправителю чатов",
            UsernameInvalid: "Некорректный адрес группы",
        }.get(type(exc), "Нет доступа к отправке в группу")
        await storage.record_group_failure(cid, gid, reason, pause=True)
        action = "Приостановлена только эта группа. Проверьте доступ и включите её в карточке группы. Остальные группы продолжают рассылку."
    elif isinstance(exc, (TimeoutError, ConnectionError, OSError, DeliveryUncertainError)):
        reason = "Сетевой сбой: результат отправки не подтверждён"
        await storage.record_group_failure(cid, gid, reason, pause=True)
        action = "Приостановлена только эта группа, чтобы не повторить уже доставленный пост. Проверьте чат перед повторным включением. Остальные группы продолжают рассылку."
    else:
        reason = f"Ошибка отправки ({type(exc).__name__})"
        await storage.record_group_failure(cid, gid, reason)
        action = "Пост остаётся в очереди этой группы до следующей попытки по интервалу. Остальные группы продолжают рассылку."
    logger.warning("[Campaign %s] Group %s: %s", cid, gid, reason)
    await notify_group_error(admin_bot, campaign, group, reason, action, type(exc).__name__)


async def flood_wait_remaining(user_id):
    if user_id in accounts.failed_flood_writes:
        return 2**31 - 1
    return await db.account_flood_remaining(user_id)


def normalize_group_destination(group_link):
    value = group_link.replace("https://t.me/", "").replace("@", "")
    if value.startswith("-") and value[1:].isdigit():
        return int(value)
    return value


async def run_campaign_spam(campaign_id: int, admin_bot=None, campaign_context=None):
    async def waiting(campaign):
        logger.warning("Campaign %s waiting for database recovery", campaign_id)
        if campaign:
            await send_notice(admin_bot, campaign["owner_id"],
                f"⏳ Кампания <b>{escape(campaign['name'][:200])}</b>: техническое ожидание базы. "
                "Незавершённая запись очереди или расписания удерживается в памяти. Отправка не повторяется; после сохранения работа продолжится автоматически.")
    async def recovered(campaign):
        logger.info("Campaign %s: database recovered", campaign_id)
        if campaign:
            await send_notice(admin_bot, campaign["owner_id"],
                f"✅ Кампания <b>{escape(campaign['name'][:200])}</b>: база снова доступна, незавершённые записи сохранены. "
                "Если кампания не остановлена, рассылка продолжится автоматически.")
    def saved(name, args):
        if name == "stop_account_for_flood" and args[0] in account_flood_pending:
            account_flood_pending.discard(args[0])
            accounts.failed_flood_writes.discard(args[0])
    storage = campaign_storage_sessions.get(campaign_id)
    if storage is None or storage.database is not db:
        campaign_storage_waiting.discard(campaign_id)
        storage = CampaignStorage(db, campaign_id, campaign_storage_waiting, waiting, recovered, saved)
        campaign_storage_sessions[campaign_id] = storage
    else:
        storage.on_wait, storage.on_recovered = waiting, recovered
        storage.on_saved = saved
    if storage.campaign is None and campaign_context:
        storage.campaign = campaign_context
    try:
        await _run_campaign_spam(campaign_id, admin_bot, storage)
    finally:
        campaign_sending.pop(campaign_id, None)
        if storage.pending:
            # Keep idempotent pending writes if the worker itself is cancelled.
            campaign_storage_waiting.add(campaign_id)
        else:
            campaign_storage_sessions.pop(campaign_id, None)
            campaign_storage_waiting.discard(campaign_id)


async def _run_campaign_spam(campaign_id: int, admin_bot, storage):
    """Send to each group when its independently persisted interval is due."""
    while True:
        campaign = await storage.get_campaign(campaign_id)
        if not campaign or not campaign["is_running"]:
            await storage.checkpoint()
            logger.info("⏹ [Campaign %s] Stopped", campaign_id)
            return
        messages = await storage.get_campaign_messages(campaign_id)
        groups = [group for group in await storage.get_campaign_groups(campaign_id) if group["is_enabled"]]
        await storage.checkpoint()
        if not messages or not groups:
            await asyncio.sleep(5)
            continue

        now = time.time()
        due_groups = sorted((group for group in groups if max(group["next_attempt_at"], group["retry_after"]) <= now), key=lambda group: group["id"])
        if not due_groups:
            earliest = min(max(group["next_attempt_at"], group["retry_after"]) for group in groups)
            await asyncio.sleep(min(5, max(0.1, earliest - now)))
            continue

        for group in due_groups:
            group_link = group["group_link"]
            destination = normalize_group_destination(group_link)
            reserved = False
            try:
                async with accounts.lock(campaign["owner_id"]):
                    campaign = await storage.get_campaign(campaign_id)
                    if not campaign or not campaign["is_running"]:
                        return
                    if campaign["owner_id"] in accounts.failed_flood_writes or await storage.account_flood_remaining(campaign["owner_id"]):
                        return
                    try:
                        # Connecting may use the client's session SQLite database,
                        # but does not send a campaign post, so retrying is safe.
                        client = await storage.retry_client(lambda: accounts.get_client(campaign["owner_id"]))
                    except (sqlite3.Error, PeerFlood, UserRestricted, Unauthorized):
                        raise
                    except Exception as exc:
                        await storage.set_campaign_running(campaign_id, False, reason="account")
                        logger.warning("[Campaign %s] Could not connect account: %s", campaign_id, type(exc).__name__)
                        await send_notice(admin_bot, campaign["owner_id"],
                            f"🛑 Кампания <b>{escape(campaign['name'][:200])}</b> остановлена: не удалось подключить отправляющий аккаунт. Проверьте подключение через /start.")
                        return
                    if not client:
                        await storage.set_campaign_running(campaign_id, False, reason="account")
                        await send_notice(admin_bot, campaign["owner_id"],
                            f"🛑 Кампания <b>{escape(campaign['name'][:200])}</b> остановлена: отправляющий аккаунт не подключён. Подключите его через /start.")
                        return

                    # Read fresh content and the group's persisted rotation under the account lock.
                    message = await storage.get_next_group_message(campaign_id, group["id"])
                    if not message:
                        await storage.record_group_failure(campaign_id, group["id"], "Нет подходящих вариантов постов для этой группы", pause=True)
                        await notify_group_error(admin_bot, campaign, group, "Нет подходящих вариантов постов для этой группы",
                            "Приостановлена только эта группа. Добавьте подходящий пост и включите группу в её карточке.", "no_compatible_posts")
                        continue

                    reserved = await storage.reserve_group_attempt(campaign_id, group["id"], time.time())
                    if not reserved:
                        continue
                    campaign_sending[campaign_id] = group["id"]
                    delivered_id = None
                    try:
                        if isinstance(destination, int) and destination < 0:
                            await storage.retry_client(lambda: ensure_group_peer(client, destination))

                        async def still_running():
                            current = await storage.get_campaign(campaign_id)
                            target = await storage.get_group(campaign_id, group["id"])
                            await storage.checkpoint()
                            return bool(current and current["is_running"] and target and target["is_enabled"])

                        try:
                            sent = await send_campaign_message(client, message, destination, admin_bot, still_running)
                        except sqlite3.Error as exc:
                            raise DeliveryUncertainError("Telegram session persistence failed") from exc
                        if not sent:
                            continue
                        delivered_id = message["id"]
                        logger.info("✅ [Campaign %s] Sent msg#%s → %s", campaign_id, message["id"], destination)
                    except FloodWait as exc:
                        if campaign["owner_id"] not in accounts.failed_flood_writes:
                            accounts.failed_flood_writes.add(campaign["owner_id"])
                            account_flood_pending.add(campaign["owner_id"])
                        try:
                            await storage.stop_account_for_flood(campaign["owner_id"], exc.value)
                        except Exception:
                            accounts.failed_flood_writes.add(campaign["owner_id"])
                            logger.exception("Could not persist FloodWait; account remains blocked")
                        logger.warning("Telegram FloodWait %ss: account campaigns stopped", exc.value)
                        await notify_group_error(admin_bot, campaign, group,
                            f"Telegram ограничил весь отправляющий аккаунт на {exc.value} сек.",
                            "Остановлены кампании этого аккаунта. Это не локальная ошибка группы; после окончания ограничения нужен ручной запуск.", "FloodWait")
                        return
                    finally:
                        campaign_sending.pop(campaign_id, None)
                        await storage.complete_group_attempt(campaign_id, group["id"], time.time(), delivered_id)
                        await storage.checkpoint()
            except sqlite3.Error:
                raise
            except (PeerFlood, UserRestricted, Unauthorized) as exc:
                accounts.failed_flood_writes.add(campaign["owner_id"])
                await storage.stop_account_campaigns(campaign["owner_id"], "restricted")
                await notify_group_error(admin_bot, campaign, group, f"Ограничение или потеря авторизации всего аккаунта ({type(exc).__name__})",
                    "Кампании этого аккаунта остановлены. Проверьте аккаунт в Telegram; это не ошибка одной группы.", type(exc).__name__)
                return
            except Exception as exc:
                await handle_group_error(exc, campaign, group, admin_bot, storage)
                await storage.checkpoint()

            if reserved:
                # Space out different groups without exceeding a shorter configured interval.
                await asyncio.sleep(min(10, campaign["min_delay"]))


async def ensure_group_peer(client, chat_id):
    try:
        await client.storage.get_peer_by_id(chat_id)
        return
    except KeyError:
        pass
    for archived in (False, True):
        async for dialog in client.get_dialogs(from_archive=archived):
            if dialog.chat.id == chat_id:
                await client.storage.save()
                return
    raise PeerIdInvalid()


async def send_campaign_message(client, message, chat_id, admin_bot=None, still_running=None):
    entities = sender_entities(message.get("entities_json"))
    if message['media_type'] in ('photo', 'video', 'video_note'):
        if not admin_bot:
            raise RuntimeError("Admin bot required to download media")
        media = BytesIO()
        media.name = "photo.jpg" if message['media_type'] == 'photo' else "video.mp4"
        await admin_bot.download(message['media_id'], destination=media)
        media.seek(0)
        if still_running and not await still_running():
            return False
        if message['media_type'] == 'photo':
            await client.send_photo(chat_id=chat_id, photo=media, caption=message['text'], caption_entities=entities, parse_mode=enums.ParseMode.DISABLED)
        elif message['media_type'] == 'video':
            await client.send_video(chat_id=chat_id, video=media, caption=message['text'], caption_entities=entities, parse_mode=enums.ParseMode.DISABLED)
        else:
            metadata = json.loads(message.get("media_metadata") or "{}")
            await client.send_video_note(chat_id=chat_id, video_note=media, duration=metadata.get("duration", 0), length=metadata.get("length", 384))
    else:
        if still_running and not await still_running():
            return False
        await client.send_message(chat_id=chat_id, text=message['text'], entities=entities, parse_mode=enums.ParseMode.DISABLED)
    return True


async def campaign_monitor(admin_bot=None):
    """
    Monitor all campaigns and spawn/stop tasks as needed.
    This runs continuously and manages individual campaign tasks.
    """
    logger.info("👀 Campaign monitor started")
    
    while True:
        try:
            # Get all running campaigns
            running_campaigns = await db.get_running_campaigns()
            running_ids = {c['id'] for c in running_campaigns}
            
            # Start tasks for newly running campaigns
            for campaign in running_campaigns:
                campaign_id = campaign['id']
                
                # Check if task already exists and is running
                if campaign_id in campaign_tasks:
                    if not campaign_tasks[campaign_id].done():
                        continue  # Already running
                    else:
                        # Task finished, remove it
                        del campaign_tasks[campaign_id]
                
                # Start new task for this campaign
                logger.info(f"▶️ Starting task for campaign {campaign_id}: '{campaign['name']}'")
                task = asyncio.create_task(run_campaign_spam(campaign_id, admin_bot, campaign_context=campaign))
                campaign_tasks[campaign_id] = task
            
            # Stop tasks for campaigns that are no longer running
            stopped_ids = set(campaign_tasks.keys()) - running_ids
            for campaign_id in stopped_ids:
                task = campaign_tasks[campaign_id]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        logger.info(f"⏹ Cancelled task for campaign {campaign_id}")
                del campaign_tasks[campaign_id]
            
        except Exception as e:
            logger.error(f"Error in campaign monitor: {e}", exc_info=True)
        
        # Check every 2 seconds
        await asyncio.sleep(2)
