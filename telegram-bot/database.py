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
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_copied_msg
            ON copied_files(source_chat_id, dest_chat_id, source_msg_id)
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_copied_doc
            ON copied_files(source_chat_id, dest_chat_id, doc_id)
            WHERE doc_id IS NOT NULL
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER UNIQUE NOT NULL,
                username   TEXT,
                added_by   INTEGER,
                added_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

# ── Forward rules ─────────────────────────────────────────────────────────────

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
        cursor = await db.execute("SELECT * FROM forward_rules WHERE active=1")
        return [dict(row) for row in await cursor.fetchall()]

# ── Ignore list ───────────────────────────────────────────────────────────────

async def add_ignore(user_id: int, chat_id: int, chat_name: str) -> bool:
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM ignore_list")
        return [dict(row) for row in await cursor.fetchall()]

# ── Dedup (copied files) ──────────────────────────────────────────────────────

async def mark_copied(source_chat_id: int, dest_chat_id: int, source_msg_id: int, doc_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO copied_files "
            "(source_chat_id, dest_chat_id, source_msg_id, doc_id) VALUES (?,?,?,?)",
            (source_chat_id, dest_chat_id, source_msg_id, doc_id)
        )
        await db.commit()

async def load_copied_ids(source_chat_id: int, dest_chat_id: int):
    """Return (msg_id_set, doc_id_set) for a source→dest pair.
    Named load_copied_ids to match the import in userbot/forwarder.py.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT source_msg_id, doc_id FROM copied_files "
            "WHERE source_chat_id=? AND dest_chat_id=?",
            (source_chat_id, dest_chat_id)
        )
        rows = await cursor.fetchall()
    msg_ids = {r["source_msg_id"] for r in rows}
    doc_ids = {r["doc_id"] for r in rows if r["doc_id"] is not None}
    return msg_ids, doc_ids

async def mark_copied_batch(source_chat_id: int, dest_chat_id: int, batch: list):
    if not batch:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO copied_files "
            "(source_chat_id, dest_chat_id, source_msg_id, doc_id) VALUES (?,?,?,?)",
            [(source_chat_id, dest_chat_id, msg_id, doc_id) for msg_id, doc_id in batch]
        )
        await db.commit()

async def get_copied_count(source_chat_id: int, dest_chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM copied_files WHERE source_chat_id=? AND dest_chat_id=?",
            (source_chat_id, dest_chat_id)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

# ── Admin management ──────────────────────────────────────────────────────────

async def load_admin_ids() -> set:
    """Return a set of all admin user_ids from the DB (does NOT include the owner)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM admins")
        rows = await cur.fetchall()
    return {r[0] for r in rows}

async def list_admins() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM admins ORDER BY added_at ASC")
        return [dict(r) for r in await cur.fetchall()]

async def add_admin(user_id: int, username: str = None, added_by: int = None) -> bool:
    """Returns True if added, False if already exists."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM admins WHERE user_id=?", (user_id,))
        if await cur.fetchone() is not None:
            return False
        await db.execute(
            "INSERT INTO admins (user_id, username, added_by) VALUES (?,?,?)",
            (user_id, username, added_by)
        )
        await db.commit()
        return True

async def remove_admin(user_id: int) -> bool:
    """Returns True if removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()
        return cur.rowcount > 0

async def update_admin_username(user_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE admins SET username=? WHERE user_id=?",
            (username, user_id)
        )
        await db.commit()
