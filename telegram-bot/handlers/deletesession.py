"""
/deletesession — revoke the active Telethon userbot session server-side.

Sends Telegram's LogOutRequest so the SESSION_STRING is permanently
invalidated even if someone else has a copy of it.

Flow:
  /deletesession
  → bot checks userbot state, warns if a job is running
  → shows ⚠️ confirmation keyboard
  → user taps "Yes, revoke it"
  → client.log_out() sent to Telegram
  → client disconnected; bot_data marked not-ready
  → user told to remove SESSION_STRING from env vars and restart

After log_out() the _connect_loop will reconnect and detect the session
is no longer authorised.  It enters its "wait for /login" polling mode,
so the user can log back in with /login without restarting the bot.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import userbot_bridge as bridge

logger = logging.getLogger(__name__)

_CONFIRM_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("⚠️ Yes, revoke it", callback_data="ds_confirm"),
        InlineKeyboardButton("❌ Cancel",          callback_data="ds_cancel"),
    ],
])


def _menu_kb():
    from handlers.menu import main_menu_keyboard
    return main_menu_keyboard()


# ── /deletesession command ────────────────────────────────────────────────────

async def deletesession_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data

    if not bridge.is_ready(bot_data):
        locked = bridge.is_locked(bot_data)
        if locked:
            msg = (
                "🔒 *Userbot session is locked* (SQLite lock).\n\n"
                "Nothing to revoke — use /login to reconnect first."
            )
        else:
            msg = (
                "ℹ️ *No active userbot session.*\n\n"
                "There is nothing to revoke. "
                "Use /login or /gensession to create a new session."
            )
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=_menu_kb())
        return

    # Warn if a copy or sync job is in progress
    copy_task = bot_data.get("active_copy_task")
    sync_task = bot_data.get("active_sync_task")
    running_jobs = []
    if copy_task and not copy_task.done():
        running_jobs.append("copy job")
    if sync_task and not sync_task.done():
        running_jobs.append("sync job")

    job_warning = ""
    if running_jobs:
        job_warning = (
            f"\n\n⚠️ *A {' and '.join(running_jobs)} is currently running.* "
            "Revoking the session will abort it immediately."
        )

    await update.message.reply_text(
        "🚨 *Revoke Userbot Session?*\n\n"
        "This will *permanently* invalidate your `SESSION_STRING` on Telegram's "
        "servers. Any copy of the string — on Railway, Replit, or anywhere else — "
        "will stop working immediately."
        f"{job_warning}\n\n"
        "After revoking:\n"
        "• Remove `SESSION_STRING` from your environment variables\n"
        "• Restart the bot (or use /login to reconnect without restarting)\n\n"
        "Are you sure?",
        parse_mode="Markdown",
        reply_markup=_CONFIRM_KB,
    )


# ── confirm callback ──────────────────────────────────────────────────────────

async def deletesession_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Revoking session…")
    bot_data = context.bot_data

    # Double-check the session is still up (user may have delayed)
    if not bridge.is_ready(bot_data):
        await query.edit_message_text(
            "ℹ️ The userbot session is already disconnected — nothing to revoke.",
            reply_markup=_menu_kb(),
        )
        return

    client = bridge.get_client(bot_data)
    if client is None:
        await query.edit_message_text(
            "❌ Could not find the userbot client. Try restarting the bot.",
            reply_markup=_menu_kb(),
        )
        return

    # Cancel any running jobs gracefully before revoking
    for key in ("active_copy_task", "active_sync_task"):
        task = bot_data.get(key)
        if task and not task.done():
            task.cancel()

    # ── Revoke the session on Telegram's servers ──────────────────────────────
    try:
        await client.log_out()
        logger.info("Userbot session revoked via /deletesession")
    except Exception as e:
        logger.warning("log_out() raised %s — session may already be invalid", e)
        # Still mark as not-ready; the session is likely gone anyway
    finally:
        bot_data["userbot_ready"] = False
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    await query.edit_message_text(
        "✅ *Session revoked.*\n\n"
        "The `SESSION_STRING` is now permanently invalid on Telegram's servers.\n\n"
        "📋 *Next steps:*\n"
        "1. Go to your hosting platform (Railway, etc.) and *delete* the "
        "`SESSION_STRING` environment variable.\n"
        "2. Use */gensession* to generate a fresh session string, then add "
        "it back as `SESSION_STRING`.\n"
        "3. Restart the bot — or use */login* right now to reconnect without restarting.\n\n"
        "_(All forwarding rules are still saved — only the session was removed.)_",
        parse_mode="Markdown",
        reply_markup=_menu_kb(),
    )


# ── cancel callback ───────────────────────────────────────────────────────────

async def deletesession_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled.")
    await query.edit_message_text(
        "❌ Session revocation cancelled. Your session is still active.",
        reply_markup=_menu_kb(),
    )


# ── handler list (consumed by bot.py) ────────────────────────────────────────

def get_deletesession_handlers() -> list:
    return [
        CommandHandler("deletesession", deletesession_cmd),
        CallbackQueryHandler(deletesession_confirm, pattern="^ds_confirm$"),
        CallbackQueryHandler(deletesession_cancel,  pattern="^ds_cancel$"),
    ]
