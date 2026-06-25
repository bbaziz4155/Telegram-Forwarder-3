"""
/purgedups <channel_id_or_username>

Scan the destination channel, find duplicate file messages, and delete all
but the OLDEST copy of each file (identified by filename + filesize).

Usage: /purgedups -1001234567890
       /stoppurge  — cancel a running purge job
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
import userbot_bridge as bridge

logger = logging.getLogger(__name__)

_PURGE_CANCEL_KEY = "purge_cancel"


async def _run_purgedups(
    client, dest_entity, dest_name,
    bot, chat_id, status_msg_id, bot_data,
):
    """Background coroutine: scan + delete duplicates without blocking the bot."""

    async def _edit(text):
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=status_msg_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    def _cleanup():
        """Release the task slot and cancel flag at every exit path."""
        bot_data.pop(_PURGE_CANCEL_KEY, None)
        bot_data["active_purge_task"] = None

    seen: dict = {}
    dups: list = []
    scanned = 0

    # ── First pass: scan for duplicates ───────────────────────────────────────
    try:
        async for msg in client.iter_messages(dest_entity, reverse=True):
            # Cooperative cancellation check
            if bot_data.get(_PURGE_CANCEL_KEY):
                bot_data[_PURGE_CANCEL_KEY] = False
                await _edit(
                    f"🛑 *Purge cancelled.*\n\n"
                    f"Scanned `{scanned:,}` messages — `{len(dups):,}` duplicates found, none deleted."
                )
                _cleanup()
                return

            scanned += 1
            f = getattr(msg, "file", None)
            if f and getattr(f, "name", None) and getattr(f, "size", None):
                key = (f.name, f.size)
                if key in seen:
                    dups.append(msg.id)   # newer copy — queue for deletion
                else:
                    seen[key] = msg.id    # oldest copy — keep it

            if scanned % 1000 == 0:
                await _edit(
                    f"🔍 Scanning *{dest_name}*…\n"
                    f"Scanned: `{scanned:,}` messages | Duplicates: `{len(dups):,}`\n\n"
                    f"_Send /stoppurge to cancel._"
                )
                await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        await _edit(
            f"⚠️ Scan interrupted after `{scanned:,}` messages.\n"
            f"Found `{len(dups):,}` duplicates — none deleted."
        )
        _cleanup()
        return
    except Exception as e:
        await _edit(f"❌ Scan error after `{scanned:,}` messages:\n`{e}`")
        _cleanup()
        return

    if not dups:
        await _edit(
            f"✅ *No duplicates found* in *{dest_name}*!\n"
            f"Scanned `{scanned:,}` messages — every file is unique."
        )
        _cleanup()
        return

    await _edit(
        f"🗑 Found *{len(dups):,}* duplicate messages in *{dest_name}*.\n"
        f"Scanned `{scanned:,}` total messages.\n\n"
        f"Deleting now… _Send /stoppurge to cancel._"
    )

    # ── Second pass: delete duplicates in batches of 100 ──────────────────────
    deleted    = 0
    failed_del = 0

    for i in range(0, len(dups), 100):
        # Cancellation check between batches
        if bot_data.get(_PURGE_CANCEL_KEY):
            bot_data[_PURGE_CANCEL_KEY] = False
            await _edit(
                f"🛑 *Purge cancelled.*\n\n"
                f"📊 Scanned: `{scanned:,}` | Found: `{len(dups):,}` | "
                f"Deleted: `{deleted:,}`"
            )
            _cleanup()
            return

        batch = dups[i : i + 100]
        try:
            await client.delete_messages(dest_entity, batch)
            deleted += len(batch)
        except Exception as batch_err:
            logger.warning("purgedups: batch delete failed: %s", batch_err)
            # Retry one by one
            for mid in batch:
                try:
                    await client.delete_messages(dest_entity, [mid])
                    deleted += 1
                except Exception:
                    failed_del += 1

        # Progress update every ~500 deletions
        if deleted % 500 < 100 or i + 100 >= len(dups):
            await _edit(
                f"🗑 Deleting duplicates from *{dest_name}*…\n"
                f"Deleted: `{deleted:,}` / `{len(dups):,}`\n\n"
                f"_Send /stoppurge to cancel._"
            )

        await asyncio.sleep(0.5)   # gentle pacing to avoid flood waits

    lines = [
        f"✅ *Deduplication complete* for *{dest_name}*!",
        "",
        f"📊 Scanned:         `{scanned:,}` messages",
        f"🗑 Duplicates found: `{len(dups):,}`",
        f"✅ Deleted:          `{deleted:,}`",
    ]
    if failed_del:
        lines.append(f"❌ Failed to delete:  `{failed_del:,}`")

    _cleanup()
    await _edit("\n".join(lines))


async def purgedups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🗑 *Purge Duplicates*\n\n"
            "Usage: `/purgedups <channel\_id\_or\_username>`\n\n"
            "Scans the destination channel, finds duplicate file messages "
            "\\(same filename \\+ filesize\\), and deletes all but the *oldest* copy\\.\n\n"
            "Example:\n`/purgedups \\-1001234567890`\n\n"
            "Send /stoppurge to cancel a running job\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Only one purge at a time
    existing = context.bot_data.get("active_purge_task")
    if existing and not existing.done():
        await update.message.reply_text(
            "⚠️ *A purge job is already running.*\n\n"
            "Send /stoppurge to cancel it first.",
            parse_mode="Markdown",
        )
        return

    dest_arg = args[0]
    chat_id  = update.effective_chat.id

    if not bridge.is_ready(context.bot_data):
        await update.message.reply_text(
            "❌ Userbot not connected. Use /login first."
        )
        return

    client = bridge.get_client(context.bot_data)

    status_msg = await update.message.reply_text(
        f"🔍 Resolving channel `{dest_arg}`…",
        parse_mode="Markdown",
    )

    # Resolve entity
    try:
        dest_raw    = int(dest_arg) if dest_arg.lstrip("-").isdigit() else dest_arg
        dest_entity = await client.get_entity(dest_raw)
    except Exception as e:
        await status_msg.edit_text(f"❌ Could not resolve channel: `{e}`", parse_mode="Markdown")
        return

    dest_name = getattr(dest_entity, "title", str(dest_arg))

    await status_msg.edit_text(
        f"🔍 Scanning *{dest_name}* for duplicate files…\n"
        f"This may take a while for large channels.\n\n"
        f"_Send /stoppurge to cancel at any time._",
        parse_mode="Markdown",
    )

    context.bot_data[_PURGE_CANCEL_KEY] = False
    task = asyncio.create_task(
        _run_purgedups(
            client, dest_entity, dest_name,
            context.application.bot, chat_id, status_msg.message_id,
            context.bot_data,
        )
    )
    context.bot_data["active_purge_task"] = task


async def stoppurge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stoppurge — cancel a running /purgedups job."""
    task = context.bot_data.get("active_purge_task")
    if task and not task.done():
        context.bot_data[_PURGE_CANCEL_KEY] = True
        await update.message.reply_text(
            "🛑 *Cancelling purge…*\n\n"
            "It will stop after the current batch completes.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No purge job is currently running.",
            parse_mode="Markdown",
        )


def get_purgedups_handlers():
    return [
        CommandHandler("purgedups", purgedups_cmd),
        CommandHandler("stoppurge", stoppurge_cmd),
    ]
