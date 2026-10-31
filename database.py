import aiosqlite
import logging
import random
import time
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

DB_PATH = "spam_bot.db"


class Database:
    """Async SQLite database manager for multi-campaign spam bot."""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
    
    async def init_db(self, owner_id: int):
        """Initialize database and create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            # Check if old schema exists for migration
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='post'"
            )
            old_schema_exists = await cursor.fetchone() is not None
            
            # Campaigns table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER,
                    name TEXT NOT NULL,
                    min_delay INTEGER DEFAULT 60,
                    max_delay INTEGER DEFAULT 120,
                    is_running BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor = await db.execute("PRAGMA table_info(campaigns)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "owner_id" not in columns:
                await db.execute("ALTER TABLE campaigns ADD COLUMN owner_id INTEGER")
            
            # Messages table (multiple variants per campaign)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    text TEXT,
                    media_type TEXT,
                    media_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
                )
            """)
            
            # Groups table (linked to campaigns)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    group_link TEXT NOT NULL,
                    last_attempt_at REAL,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
                    UNIQUE(campaign_id, group_link)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id INTEGER PRIMARY KEY,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE TABLE IF NOT EXISTS runtime_safety (id INTEGER PRIMARY KEY CHECK (id = 1), flood_until REAL NOT NULL DEFAULT 0)")
            await db.execute("INSERT OR IGNORE INTO runtime_safety (id, flood_until) VALUES (1, 0)")
            await db.execute("""CREATE TABLE IF NOT EXISTS account_sessions (
                user_id INTEGER PRIMARY KEY,
                telegram_id INTEGER NOT NULL UNIQUE,
                flood_until REAL NOT NULL DEFAULT 0
            )""")
            
            await db.commit()
            
            # Migrate old data if old schema exists
            if old_schema_exists:
                await self._migrate_old_data(db)

            cursor = await db.execute("PRAGMA table_info(groups)")
            group_columns = {row[1] for row in await cursor.fetchall()}
            if "last_attempt_at" not in group_columns:
                await db.execute("ALTER TABLE groups ADD COLUMN last_attempt_at REAL")
            if "next_attempt_at" not in group_columns:
                await db.execute("ALTER TABLE groups ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")

            await db.execute(
                "UPDATE campaigns SET owner_id = ? WHERE owner_id IS NULL", (owner_id,)
            )
            await db.execute("UPDATE campaigns SET is_running = 0 WHERE is_running != 0")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_campaigns_owner ON campaigns(owner_id)"
            )
            await db.commit()
            
            logger.info("Database initialized successfully")

    async def is_authorized_user(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM authorized_users WHERE user_id = ?", (user_id,)
            )
            return await cursor.fetchone() is not None

    async def get_authorized_users(self) -> List[int]:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM authorized_users ORDER BY user_id"
            )
            return [row[0] for row in await cursor.fetchall()]

    async def add_authorized_user(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO authorized_users (user_id) VALUES (?)", (user_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def remove_authorized_user(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "DELETE FROM authorized_users WHERE user_id = ?", (user_id,)
            )
            await conn.execute("UPDATE campaigns SET is_running = 0 WHERE owner_id = ?", (user_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def stop_for_flood_wait(self, seconds: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("UPDATE campaigns SET is_running = 0 WHERE is_running != 0")
            await conn.execute(
                "UPDATE runtime_safety SET flood_until = MAX(flood_until, ?) WHERE id = 1",
                (time.time() + max(0, seconds),),
            )
            await conn.commit()

    async def flood_wait_remaining(self) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT flood_until FROM runtime_safety WHERE id = 1")
            row = await cursor.fetchone()
            return max(0, int((row[0] if row else 0) - time.time() + 0.999))

    async def save_account(self, user_id: int, telegram_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT INTO account_sessions (user_id, telegram_id) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET telegram_id=excluded.telegram_id", (user_id, telegram_id))
            await conn.commit()

    async def get_account(self, user_id: int):
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT telegram_id FROM account_sessions WHERE user_id = ?", (user_id,))
            return await cursor.fetchone()

    async def stop_account_for_flood(self, user_id: int, seconds: int):
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("UPDATE campaigns SET is_running = 0 WHERE owner_id = ?", (user_id,))
            await conn.execute("UPDATE account_sessions SET flood_until = MAX(flood_until, ?) WHERE user_id = ?", (time.time() + max(0, seconds), user_id))
            await conn.commit()

    async def account_flood_remaining(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT flood_until FROM account_sessions WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return max(0, int((row[0] if row else 0) - time.time() + 0.999))
    
    async def _migrate_old_data(self, db):
        """Migrate data from old single-campaign schema to new multi-campaign schema."""
        logger.info("🔄 Migrating old data to Default Campaign...")
        
        try:
            # Check if default campaign already exists
            cursor = await db.execute(
                "SELECT id FROM campaigns WHERE name = 'Default Campaign'"
            )
            existing = await cursor.fetchone()
            
            if existing:
                logger.info("Default Campaign already exists, skipping migration")
                return
            
            # Create default campaign
            cursor = await db.execute(
                "INSERT INTO campaigns (name, min_delay, max_delay, is_running) VALUES (?, ?, ?, ?)",
                ("Default Campaign", 60, 120, 0)
            )
            campaign_id = cursor.lastrowid
            
            # Migrate settings if they exist
            cursor = await db.execute("SELECT min_delay, max_delay FROM settings WHERE id = 1")
            settings = await cursor.fetchone()
            if settings:
                await db.execute(
                    "UPDATE campaigns SET min_delay = ?, max_delay = ? WHERE id = ?",
                    (settings[0], settings[1], campaign_id)
                )
            
            # Migrate post if it exists
            cursor = await db.execute("SELECT text, media_type, file_id FROM post WHERE id = 1")
            post = await cursor.fetchone()
            if post and post[0]:  # If text exists
                await db.execute(
                    "INSERT INTO messages (campaign_id, text, media_type, media_id) VALUES (?, ?, ?, ?)",
                    (campaign_id, post[0], post[1], post[2])
                )
            
            # Migrate groups
            cursor = await db.execute("SELECT group_link FROM groups")
            old_groups = await cursor.fetchall()
            for (group_link,) in old_groups:
                await db.execute(
                    "INSERT INTO groups (campaign_id, group_link) VALUES (?, ?)",
                    (campaign_id, group_link)
                )
            
            # Migrate running state
            cursor = await db.execute("SELECT is_running FROM state WHERE id = 1")
            state = await cursor.fetchone()
            if state:
                await db.execute(
                    "UPDATE campaigns SET is_running = ? WHERE id = ?",
                    (state[0], campaign_id)
                )
            
            await db.commit()
            
            # Drop old tables
            await db.execute("DROP TABLE IF EXISTS post")
            await db.execute("DROP TABLE IF EXISTS settings")
            await db.execute("DROP TABLE IF EXISTS state")
            # Rename old groups table
            await db.execute("ALTER TABLE groups RENAME TO groups_old")
            # Recreate groups table
            await db.execute("""
                CREATE TABLE groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    group_link TEXT NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
                    UNIQUE(campaign_id, group_link)
                )
            """)
            # Copy migrated groups
            await db.execute("""
                INSERT INTO groups (campaign_id, group_link)
                SELECT ?, group_link FROM groups_old
            """, (campaign_id,))
            await db.execute("DROP TABLE groups_old")
            
            await db.commit()
            logger.info(f"✅ Migration complete! Default Campaign ID: {campaign_id}")
            
        except Exception as e:
            logger.error(f"Migration failed: {e}", exc_info=True)
            await db.rollback()
    
    # ===== CAMPAIGN OPERATIONS =====
    
    async def create_campaign(self, name: str, owner_id: int, min_delay: int = 60, max_delay: int = 120) -> int:
        """Create a new campaign. Returns campaign ID."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO campaigns (name, owner_id, min_delay, max_delay) VALUES (?, ?, ?, ?)",
                (name, owner_id, min_delay, max_delay)
            )
            await db.commit()
            campaign_id = cursor.lastrowid
            logger.info(f"Created campaign '{name}' with ID {campaign_id}")
            return campaign_id
    
    async def get_all_campaigns(self, owner_id: int) -> List[Dict[str, Any]]:
        """Get all campaigns."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def campaign_belongs_to(self, campaign_id: int, owner_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM campaigns WHERE id = ? AND owner_id = ?",
                (campaign_id, owner_id),
            )
            return await cursor.fetchone() is not None
    
    async def get_campaign(self, campaign_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific campaign by ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE id = ?",
                (campaign_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def update_campaign_name(self, campaign_id: int, name: str):
        """Update campaign name."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE campaigns SET name = ? WHERE id = ?",
                (name, campaign_id)
            )
            await db.commit()
            logger.info(f"Updated campaign {campaign_id} name to '{name}'")
    
    async def update_campaign_delay(self, campaign_id: int, min_delay: int, max_delay: int):
        """Update campaign delay settings."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE campaigns SET min_delay = ?, max_delay = ? WHERE id = ?",
                (min_delay, max_delay, campaign_id)
            )
            cursor = await db.execute(
                "SELECT id, last_attempt_at FROM groups WHERE campaign_id = ? AND last_attempt_at IS NOT NULL",
                (campaign_id,),
            )
            for group_id, last_attempt_at in await cursor.fetchall():
                await db.execute(
                    "UPDATE groups SET next_attempt_at = ? WHERE id = ?",
                    (last_attempt_at + random.randint(min_delay, max_delay), group_id),
                )
            await db.commit()
            logger.info(f"Updated campaign {campaign_id} delay: {min_delay}-{max_delay}s")
    
    async def delete_campaign(self, campaign_id: int) -> bool:
        """Delete a campaign (CASCADE deletes messages and groups). Returns True if deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                "DELETE FROM campaigns WHERE id = ?",
                (campaign_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted campaign {campaign_id}")
            return deleted
    
    async def set_campaign_running(self, campaign_id: int, is_running: bool):
        """Set campaign running state."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE campaigns SET is_running = ? WHERE id = ?",
                (1 if is_running else 0, campaign_id)
            )
            await db.commit()
            logger.info(f"Campaign {campaign_id} running state: {is_running}")
    
    async def get_running_campaigns(self) -> List[Dict[str, Any]]:
        """Get all currently running campaigns."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE is_running = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    # ===== MESSAGE OPERATIONS =====
    
    async def add_message(
        self,
        campaign_id: int,
        text: Optional[str] = None,
        media_type: Optional[str] = None,
        media_id: Optional[str] = None
    ) -> int:
        """Add a message variant to a campaign. Returns message ID."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO messages (campaign_id, text, media_type, media_id) VALUES (?, ?, ?, ?)",
                (campaign_id, text, media_type, media_id)
            )
            await db.commit()
            message_id = cursor.lastrowid
            logger.info(f"Added message {message_id} to campaign {campaign_id}")
            return message_id
    
    async def get_campaign_messages(self, campaign_id: int) -> List[Dict[str, Any]]:
        """Get all messages for a campaign."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM messages WHERE campaign_id = ? ORDER BY id",
                (campaign_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def get_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific message by ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM messages WHERE id = ?",
                (message_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def delete_message(self, message_id: int) -> bool:
        """Delete a message. Returns True if deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM messages WHERE id = ?",
                (message_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted message {message_id}")
            return deleted
    
    async def clear_campaign_messages(self, campaign_id: int) -> int:
        """Delete all messages for a campaign. Returns count of deleted messages."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM messages WHERE campaign_id = ?",
                (campaign_id,)
            )
            await db.commit()
            count = cursor.rowcount
            logger.info(f"Cleared {count} messages from campaign {campaign_id}")
            return count
    
    # ===== GROUP OPERATIONS =====

    async def reserve_group_attempt(self, campaign_id: int, group_id: int, now: float) -> bool:
        """Reserve a due attempt before network I/O to avoid immediate retries after a crash."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT c.min_delay, c.max_delay FROM groups g JOIN campaigns c ON c.id = g.campaign_id "
                "WHERE g.id = ? AND c.id = ? AND c.is_running = 1 AND g.next_attempt_at <= ?",
                (group_id, campaign_id, now),
            )
            row = await cursor.fetchone()
            if not row:
                await conn.rollback()
                return False
            await conn.execute(
                "UPDATE groups SET last_attempt_at = ?, next_attempt_at = ? WHERE id = ?",
                (now, now + random.randint(row[0], row[1]), group_id),
            )
            await conn.commit()
            return True

    async def finish_group_attempt(self, campaign_id: int, group_id: int, now: float):
        """Start the next interval when the attempt finishes, using the latest settings."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT c.min_delay, c.max_delay FROM groups g JOIN campaigns c ON c.id = g.campaign_id "
                "WHERE g.id = ? AND c.id = ?", (group_id, campaign_id),
            )
            row = await cursor.fetchone()
            if row:
                await conn.execute(
                    "UPDATE groups SET last_attempt_at = ?, next_attempt_at = ? WHERE id = ?",
                    (now, now + random.randint(row[0], row[1]), group_id),
                )
            await conn.commit()

    async def add_group_for_owner(self, campaign_id: int, owner_id: int, group_link: str):
        """Atomically check campaign ownership and insert a selected group."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT 1 FROM campaigns WHERE id = ? AND owner_id = ?", (campaign_id, owner_id)
            )
            if not await cursor.fetchone():
                await conn.rollback()
                return None
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO groups (campaign_id, group_link) VALUES (?, ?)",
                (campaign_id, group_link),
            )
            await conn.commit()
            return cursor.rowcount > 0
    
    async def add_groups(self, campaign_id: int, group_links: List[str]) -> int:
        """Add multiple groups to a campaign. Returns count of added groups."""
        added_count = 0
        async with aiosqlite.connect(self.db_path) as db:
            for link in group_links:
                try:
                    await db.execute(
                        "INSERT INTO groups (campaign_id, group_link) VALUES (?, ?)",
                        (campaign_id, link.strip())
                    )
                    added_count += 1
                except aiosqlite.IntegrityError:
                    # Group already exists in this campaign
                    logger.debug(f"Group {link} already exists in campaign {campaign_id}")
                    continue
            await db.commit()
        logger.info(f"Added {added_count} new groups to campaign {campaign_id}")
        return added_count
    
    async def get_campaign_groups(self, campaign_id: int) -> List[Dict[str, Any]]:
        """Get all groups for a campaign."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM groups WHERE campaign_id = ? ORDER BY id",
                (campaign_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def delete_group(self, group_id: int) -> bool:
        """Delete a group by its ID. Returns True if deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM groups WHERE id = ?",
                (group_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted group {group_id}")
            return deleted
    
    async def clear_campaign_groups(self, campaign_id: int) -> int:
        """Delete all groups for a campaign. Returns count of deleted groups."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM groups WHERE campaign_id = ?",
                (campaign_id,)
            )
            await db.commit()
            count = cursor.rowcount
            logger.info(f"Cleared {count} groups from campaign {campaign_id}")
            return count


# Singleton instance
db = Database()
