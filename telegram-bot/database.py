import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "forwarder.db")

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forward_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_chat_name TEXT,
                dest_chat_id INTEGER NOT NULL,
                dest_chat_name TEXT,
                active INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Unique index — safe to run on both new and existing tables.
        # Prevents duplicate (user, source, dest) rules accumulating in the DB.
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fwd_rules_unique
            ON forward_rules(user_id, source_chat_id, dest_chat_id)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ignore_list (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_name TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def add_rule(user_id: int, source_id: int, source_name: str, dest_id: int, dest_name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO forward_rules "
            "(user_id, source_chat_id, source_chat_name, dest_chat_id, dest_chat_name) "
            "VALUES (?,?,?,?,?)",
            (user_id, source_id, source_name, dest_id, dest_name)
        )
        await db.commit()
        if cursor.lastrowid:
            return cursor.lastrowid
        # Rule already existed — return its id
        cur2 = await db.execute(
            "SELECT id FROM forward_rules WHERE user_id=? AND source_chat_id=? AND dest_chat_id=?",
            (user_id, source_id, dest_id)
        )
        row = await cur2.fetchone()
        return row[0] if row else 0

async def get_rules(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM forward_rules WHERE user_id=? AND active=1 ORDER BY id DESC",
            (user_id,)
        )
        return [dict(row) for row in await cursor.fetchall()]

async def delete_rule(rule_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM forward_rules WHERE id=? AND user_id=?",
            (rule_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0

async def get_all_active_rules() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM forward_rules WHERE active=1"
        )
        return [dict(row) for row in await cursor.fetchall()]

async def add_ignore(user_id: int, chat_id: int, chat_name: str) -> bool:
    """Returns True if added, False if already in list."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM ignore_list WHERE user_id=? AND chat_id=?",
            (user_id, chat_id)
        )
        if await cur.fetchone() is not None:
            return False
        await db.execute(
            "INSERT INTO ignore_list (user_id, chat_id, chat_name) VALUES (?,?,?)",
            (user_id, chat_id, chat_name)
        )
        await db.commit()
        return True

async def get_ignore_list(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM ignore_list WHERE user_id=? ORDER BY id DESC",
            (user_id,)
        )
        return [dict(row) for row in await cursor.fetchall()]

async def remove_ignore(ignore_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM ignore_list WHERE id=? AND user_id=?",
            (ignore_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0

async def get_all_ignore_entries() -> list:
    """Load every ignore-list entry across all users — used to build the in-memory ignore_map."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, chat_id FROM ignore_list"
        )
        return [dict(row) for row in await cursor.fetchall()]
