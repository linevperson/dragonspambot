"""Retry database operations, never the Telegram send preceding them."""
import asyncio
import sqlite3


class CampaignStorage:
    WRITES = {
        "reserve_group_attempt", "complete_group_attempt", "record_group_failure",
        "set_campaign_running", "stop_account_for_flood", "stop_account_campaigns",
    }

    def __init__(self, database, campaign_id, waiting, on_wait, on_recovered, on_saved=None):
        self.database = database
        self.campaign_id = campaign_id
        self.waiting = waiting
        self.on_wait = on_wait
        self.on_recovered = on_recovered
        self.on_saved = on_saved
        self.campaign = None
        self.pending = []
        self.delay = 5

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            await self.flush()
            if name in self.WRITES:
                operation = (name, args, kwargs)
                self.pending.append(operation)
                return await self.flush()
            return await self.retry(name, args, kwargs)
        return call

    async def retry(self, name, args, kwargs):
        while True:
            try:
                result = await getattr(self.database, name)(*args, **kwargs)
                if name == "get_campaign" and result:
                    self.campaign = result
                self.delay = 5
                return result
            except sqlite3.Error:
                await self.backoff()

    async def retry_client(self, operation):
        """Only for client connection/peer lookups, never an actual post send."""
        while True:
            try:
                return await operation()
            except sqlite3.Error:
                await self.backoff()

    async def backoff(self):
        if self.campaign_id not in self.waiting:
            self.waiting.add(self.campaign_id)
            await self.on_wait(self.campaign)
        await asyncio.sleep(self.delay)
        self.delay = min(self.delay * 2, 30)

    async def flush(self):
        result = None
        while self.pending:
            name, args, kwargs = self.pending[0]
            result = await self.retry(name, args, kwargs)
            self.pending.pop(0)
            if self.on_saved:
                self.on_saved(name, args)
        return result

    async def checkpoint(self):
        """Clear waiting only at a safe point, after every pending write is saved."""
        await self.flush()
        if self.campaign_id in self.waiting:
            self.waiting.discard(self.campaign_id)
            await self.on_recovered(self.campaign)
