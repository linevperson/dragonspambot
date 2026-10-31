import asyncio
import logging
import random
import time
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict

from pyrogram import Client
from pyrogram.errors import FloodWait, ChatWriteForbidden, UserBannedInChannel, ChannelPrivate, ChatAdminRequired, SlowmodeWait
from pyrogram.errors.exceptions.bad_request_400 import PeerIdInvalid, UsernameInvalid

from database import db
from accounts import accounts

logger = logging.getLogger(__name__)

# Dictionary to track running campaign tasks
campaign_tasks: Dict[int, asyncio.Task] = {}


async def flood_wait_remaining(user_id):
    if user_id in accounts.failed_flood_writes:
        return 2**31 - 1
    return await db.account_flood_remaining(user_id)


def normalize_group_destination(group_link):
    value = group_link.replace("https://t.me/", "").replace("@", "")
    if value.startswith("-") and value[1:].isdigit():
        return int(value)
    return value


async def run_campaign_spam(campaign_id: int, admin_bot=None):
    """Send to each group when its independently persisted interval is due."""
    message_index = 0
    while True:
        campaign = await db.get_campaign(campaign_id)
        if not campaign or not campaign["is_running"]:
            logger.info("⏹ [Campaign %s] Stopped", campaign_id)
            return
        messages = await db.get_campaign_messages(campaign_id)
        groups = await db.get_campaign_groups(campaign_id)
        if not messages or not groups:
            await asyncio.sleep(5)
            continue

        now = time.time()
        due_groups = [group for group in groups if group["next_attempt_at"] <= now]
        if not due_groups:
            earliest = min(group["next_attempt_at"] for group in groups)
            await asyncio.sleep(min(5, max(0.1, earliest - now)))
            continue

        random.shuffle(due_groups)
        for group in due_groups:
            message = messages[message_index % len(messages)]
            group_link = group["group_link"]
            destination = normalize_group_destination(group_link)
            reserved = False
            try:
                async with accounts.lock(campaign["owner_id"]):
                    campaign = await db.get_campaign(campaign_id)
                    if not campaign or not campaign["is_running"]:
                        return
                    if await flood_wait_remaining(campaign["owner_id"]):
                        return
                    try:
                        client = await accounts.get_client(campaign["owner_id"])
                    except Exception as exc:
                        await db.set_campaign_running(campaign_id, False)
                        logger.warning("[Campaign %s] Could not connect account: %s", campaign_id, type(exc).__name__)
                        return
                    if not client:
                        await db.set_campaign_running(campaign_id, False)
                        return

                    reserved = await db.reserve_group_attempt(campaign_id, group["id"], time.time())
                    if not reserved:
                        continue
                    message_index += 1
                    try:
                        if isinstance(destination, int) and destination < 0:
                            await ensure_group_peer(client, destination)

                        async def still_running():
                            current = await db.get_campaign(campaign_id)
                            return bool(current and current["is_running"])

                        if not await send_campaign_message(client, message, destination, admin_bot, still_running):
                            return
                        logger.info("✅ [Campaign %s] Sent msg#%s → %s", campaign_id, message["id"], destination)
                    except FloodWait as exc:
                        try:
                            await db.stop_account_for_flood(campaign["owner_id"], exc.value)
                        except Exception:
                            accounts.failed_flood_writes.add(campaign["owner_id"])
                            logger.exception("Could not persist FloodWait; account remains blocked")
                        logger.warning("Telegram FloodWait %ss: account campaigns stopped", exc.value)
                        return
                    finally:
                        await db.finish_group_attempt(campaign_id, group["id"], time.time())
            except SlowmodeWait:
                logger.warning("⏳ [Campaign %s] SlowMode in %s", campaign_id, group_link)
            except (ChatWriteForbidden, UserBannedInChannel, ChannelPrivate, ChatAdminRequired):
                logger.error("🚫 [Campaign %s] No access to %s", campaign_id, group_link)
            except Exception as exc:
                logger.error("❓ [Campaign %s] Error in %s: %s", campaign_id, group_link, type(exc).__name__)

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
    if message['media_type'] in ('photo', 'video'):
        if not admin_bot:
            raise RuntimeError("Admin bot required to download media")
        media = BytesIO()
        media.name = "photo.jpg" if message['media_type'] == 'photo' else "video.mp4"
        await admin_bot.download(message['media_id'], destination=media)
        media.seek(0)
        if still_running and not await still_running():
            return False
        if message['media_type'] == 'photo':
            await client.send_photo(chat_id=chat_id, photo=media, caption=message['text'])
        else:
            await client.send_video(chat_id=chat_id, video=media, caption=message['text'])
    else:
        if still_running and not await still_running():
            return False
        await client.send_message(chat_id=chat_id, text=message['text'])
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
                task = asyncio.create_task(run_campaign_spam(campaign_id, admin_bot))
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
