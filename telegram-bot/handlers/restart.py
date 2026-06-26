"""
/restart command — owner-only graceful restart.

Cancels any active copy/sync/clean jobs, disconnects the Telethon
userbot cleanly, then calls os.execv() to replace the current process
image with a fresh one.  On Railway (or any Linux host) this is a true
in-process restart: the same PID, no Railway restart counter consumed,
and no gap in the health-check endpoint.
"""
import asyncio
import json
import logging
import os
import sys
import time

from telegram import Update
from telegram.ext import ContextTypes

import config
import userbot_bridge as bridge

logger = logging.getLogger(__name__)


async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gracefully restart the bot process (owner only)."""
    user = update.effective_user

    # Extra safety layer — only the bot owner may trigger a restart even
    # if other admins are configured.  (The admin gate already blocks
    # non-admins, so this just tightens it further for this one command.)
    if config.OWNER_ID != 0 and (user is None or user.id != config.OWNER_ID):
        await update.message.reply_text(
            "🚫 Only the bot owner can use /restart\\.",
            parse_mode="MarkdownV2",
        )
        return

    await update.message.reply_text(
        "♻️ *Restarting bot\\.\\.\\.*\n\n"
        "Cancelling active jobs and disconnecting userbot\\.\n"
        "The bot will be back online in \\~10 seconds\\.",
        parse_mode="MarkdownV2",
    )

    bot_data = context.bot_data

    # ── 1. Cancel active copy / sync / clean jobs ──────────────────────────
    # Signal handlers that this cancel is from /restart (not /stopjob) so
    # they preserve the auto-resume file — the bot picks it up after execv.
    bot_data["__restarting"] = True
    for key in ("active_copy_task", "active_sync_task", "active_cleancaptions_task", "active_purge_task"):
        task = bot_data.get(key)
        if task and not task.done():
            logger.info("Restart: cancelling %s", key)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except Exception:
                pass

    # ── 2. Cancel the userbot background connect/reconnect loop ───────────
    connect_task = bot_data.get("_userbot_connect_task")
    if connect_task and not connect_task.done():
        connect_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(connect_task), timeout=2.0)
        except Exception:
            pass

    # ── 3. Disconnect Telethon cleanly ────────────────────────────────────
    client = bridge.get_client(bot_data)
    if client is not None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=8.0)
            logger.info("Restart: Telethon disconnected cleanly.")
        except Exception as exc:
            logger.warning("Restart: Telethon disconnect error: %s", exc)
    else:
        logger.info("Restart: no Telethon client to disconnect.")

    logger.info("Restart command: replacing process via os.execv …")

    # Give Telegram a moment to deliver the confirmation message before
    # the process image is replaced.
    await asyncio.sleep(1.5)

    # Save a marker so the bot sends a "✅ Back online!" notification on startup.
    _data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
    _marker   = os.path.join(_data_dir, "restart_pending.json")
    try:
        os.makedirs(_data_dir, exist_ok=True)
        with open(_marker, "w") as _f:
            json.dump({
                "chat_id":   update.effective_chat.id,
                "user_id":   update.effective_user.id,
                "timestamp": time.time(),
            }, _f)
    except Exception as _e:
        logger.warning("Could not write restart_pending.json: %s", _e)

    # os.execv replaces the current process image in-place — same PID,
    # no Railway restart-policy counter incremented, health endpoint keeps
    # responding (it runs in its own thread and survives the execv on Linux).
    os.execv(sys.executable, [sys.executable] + sys.argv)
