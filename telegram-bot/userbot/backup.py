"""
Telegram Saved Messages backup — keeps checkpoint, dedup DB, and
autoresume state alive across Render free-tier restarts (or any
platform with an ephemeral filesystem).

How it works
------------
  Startup  → waits for the Telethon userbot to connect, then downloads
             the most recent backup zip from Saved Messages and extracts
             it into DATA_DIR before any copy job can claim the state.
  Periodic → every BACKUP_INTERVAL_SECS (default 15 min) a fresh zip is
             uploaded to Saved Messages, replacing the old one.
  Manual   → call backup_now(client) directly (e.g. from a /backup command
             or at the end of a copy job).

Enabled automatically when SESSION_STRING is present (we have Telethon
anyway). Set TELEGRAM_BACKUP=0 to disable explicitly.
"""
import asyncio
import io
import logging
import os
import zipfile
from datetime import datetime

logger = logging.getLogger(__name__)

BACKUP_INTERVAL_SECS = int(os.environ.get("BACKUP_INTERVAL_SECS", "900"))  # 15 min default
_BACKUP_TAG = "#tgforwarder_backup"  # unique tag so search finds it fast

_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_DATA_DIR = os.environ.get("DATA_DIR", _DEFAULT_DATA_DIR)


def _is_enabled() -> bool:
    explicit = os.environ.get("TELEGRAM_BACKUP", "").strip().lower()
    if explicit in ("0", "false", "no"):
        return False
    # Active by default when a SESSION_STRING is configured
    return bool(os.environ.get("SESSION_STRING", "").strip())


def _zip_data_dir() -> bytes:
    """Zip the entire DATA_DIR and return the raw bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(_DATA_DIR):
            # Skip session sqlite files — auth lives in SESSION_STRING env var
            dirs[:] = [d for d in dirs if d != "sessions"]
            for fname in files:
                # Skip SQLite WAL / SHM side-files — they may be inconsistent
                if fname.endswith(("-wal", "-shm")):
                    continue
                fpath = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, _DATA_DIR)
                try:
                    zf.write(fpath, arcname)
                except OSError as e:
                    logger.debug("Backup: skipped %s — %s", fname, e)
    return buf.getvalue()


def _restore_zip(zip_bytes: bytes) -> None:
    """Extract a backup zip into DATA_DIR (overwrites existing files)."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(_DATA_DIR)


async def backup_now(client) -> bool:
    """
    Zip DATA_DIR and upload it to Telegram Saved Messages.
    Returns True on success, False on failure.
    Safe to call from any async context.
    """
    if not os.path.exists(_DATA_DIR):
        logger.info("Backup: data dir missing — nothing to upload")
        return False
    try:
        zip_bytes = await asyncio.get_running_loop().run_in_executor(None, _zip_data_dir)
        if len(zip_bytes) < 22:          # empty zip is 22 bytes
            logger.info("Backup: data dir is empty — skipping upload")
            return False

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        buf = io.BytesIO(zip_bytes)
        buf.name = "forwarder_data.zip"

        await client.send_file(
            "me",                         # Saved Messages
            buf,
            caption=(
                f"{_BACKUP_TAG}\n"
                f"📦 Forwarder backup — {timestamp}\n\n"
                "This file restores checkpoint + dedup state after a restart.\n"
                "Keep the most recent one — older ones can be deleted."
            ),
            force_document=True,
            silent=True,
        )
        logger.info("Backup: uploaded %s bytes to Saved Messages ✓", f"{len(zip_bytes):,}")
        return True
    except Exception as exc:
        logger.warning("Backup: upload failed (non-fatal): %s", exc)
        return False


async def restore_from_telegram(client) -> bool:
    """
    Search Saved Messages for the latest backup and restore it.
    Returns True if restored, False if no backup found or error.
    """
    try:
        logger.info("Backup: searching Saved Messages for latest backup…")
        found = None
        async for msg in client.iter_messages("me", search=_BACKUP_TAG, limit=20):
            if msg.document:
                found = msg
                break          # iter_messages returns newest first

        if not found:
            logger.info("Backup: no backup found — starting with a clean data dir")
            return False

        ts = found.date.strftime("%Y-%m-%d %H:%M UTC") if found.date else "unknown time"
        logger.info("Backup: found backup from %s — downloading…", ts)

        zip_bytes = await found.download_media(bytes)
        if not zip_bytes:
            logger.warning("Backup: download returned empty — skipping restore")
            return False

        await asyncio.get_running_loop().run_in_executor(None, _restore_zip, zip_bytes)
        logger.info("Backup: restored %s bytes successfully ✓", f"{len(zip_bytes):,}")
        return True
    except Exception as exc:
        logger.warning("Backup: restore failed (non-fatal, starting fresh): %s", exc)
        return False


async def run_backup_loop(bot_data: dict) -> None:
    """
    Long-running background task launched from bot.py post_init.
    Waits for the userbot to connect, restores the latest backup,
    signals ready via bot_data["backup_restored"], then backs up
    periodically.
    """
    if not _is_enabled():
        logger.info(
            "Backup: disabled (set TELEGRAM_BACKUP=1 to enable, "
            "or configure SESSION_STRING)"
        )
        bot_data["backup_restored"] = True   # don't block auto-resume
        return

    # ── Wait for the primary userbot to be ready ───────────────────────────
    import userbot_bridge        # local import to avoid circular dependency

    logger.info("Backup: waiting for userbot to connect…")
    waited = 0
    while not userbot_bridge.is_ready(bot_data):
        if waited >= 300:        # give up after 5 minutes
            logger.warning("Backup: userbot never became ready — skipping restore")
            bot_data["backup_restored"] = True
            return
        await asyncio.sleep(5)
        waited += 5

    client = userbot_bridge.get_client(bot_data)
    if not client:
        logger.warning("Backup: no Telethon client — skipping restore")
        bot_data["backup_restored"] = True
        return

    # ── Restore on startup ─────────────────────────────────────────────────
    await restore_from_telegram(client)
    bot_data["backup_restored"] = True   # signal: auto-resume may now proceed
    logger.info("Backup: restore complete — periodic backup every %ds", BACKUP_INTERVAL_SECS)

    # ── Periodic backup loop ───────────────────────────────────────────────
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_SECS)
        client = userbot_bridge.get_client(bot_data)
        if client and userbot_bridge.is_ready(bot_data):
            await backup_now(client)
        else:
            logger.debug("Backup: userbot not ready — skipping this cycle")
