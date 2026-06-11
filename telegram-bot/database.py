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
        # ── Persistent deduplication table ────────────────────────────────────
        # Records every message that was successfully copied so that:
        #   1. Re-running /copy on a finished channel skips already-sent msgs.
        #   2. The same file re-uploaded in the source at a NEW message ID is
        #      also detected and skipped via its Telegram document ID (doc_id).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS copied_files (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                dest_chat_id   INTEGER NOT NULL,
                source_msg_id  INTEGER NOT NULL,
                doc_id         INTEGER,
                copied_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Unique on message ID — prevents double-copy of same message
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_copied_msg
            ON copied_files(source_chat_id, dest_chat_id, source_msg_id)
        """)
        # Unique on document/file ID — prevents double-copy of same file content.
        # The WHERE clause makes it a partial index so NULL doc_ids (text posts)
        # don't collide with each other.
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_copied_doc
            ON copied_files(source_chat_id, dest_chat_id, doc_id)
            WHERE doc_id IS NOT NULL
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

# ── Deduplication helpers ─────────────────────────────────────────────────────

async def load_copied_ids(
    source_chat_id: int, dest_chat_id: int
) -> tuple[set[int], set[int]]:
    """
    Load all previously copied entries for a (source, dest) pair from the DB.

    Returns:
        msg_ids  — set of source message IDs already copied
        doc_ids  — set of Telegram document IDs already copied (content dedup)

    Called once at the start of copy_channel_files so the hot-path dedup
    check is a pure in-memory set lookup (O(1), no DB round-trip per message).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT source_msg_id, doc_id FROM copied_files "
            "WHERE source_chat_id=? AND dest_chat_id=?",
            (source_chat_id, dest_chat_id),
        )
        msg_ids: set[int] = set()
        doc_ids: set[int] = set()
        async for row in cursor:
            msg_ids.add(row[0])
            if row[1] is not None:
                doc_ids.add(row[1])
        return msg_ids, doc_ids


async def mark_copied_batch(
    source_chat_id: int,
    dest_chat_id: int,
    entries: list[tuple[int, "int | None"]],
) -> None:
    """
    Persist a batch of (source_msg_id, doc_id) pairs to the DB.

    Uses INSERT OR IGNORE so concurrent or repeated calls are safe.
    Called every SAVE_EVERY messages and once at job completion.

    entries: list of (source_msg_id, doc_id_or_None)
    """
    if not entries:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO copied_files "
            "(source_chat_id, dest_chat_id, source_msg_id, doc_id) "
            "VALUES (?,?,?,?)",
            [(source_chat_id, dest_chat_id, msg_id, doc_id) for msg_id, doc_id in entries],
        )
        await db.commit()
