"""
/purgedups <channel_id_or_username>

Scan the destination channel, find duplicate file messages, and delete all
but the OLDEST copy of each file (identified by filename + filesize).

Usage: /purgedups -1001234567890
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


async def purgedups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🗑 *Purge Duplicates*\n\n"
            "Usage: `/purgedups <channel\\_id\\_or\\_username>`\n\n"
            "Scans the destination channel, finds duplicate file messages "
            "\\(same filename \\+ filesize\\), and deletes all but the *oldest* copy\\.\n\n"
            "Example:\n`/purgedups \\-1001234567890`",
            parse_mode="MarkdownV2",
        )
        return

    dest_arg = args[0]
    chat_id = update.effective_chat.id

    client = context.bot_data.get("userbot_client")
    if not client or not context.bot_data.get("userbot_ready"):
        await update.message.reply_text(
            "❌ Userbot not connected. Use /login first."
        )
        return

    status_msg = await update.message.reply_text(
        f"🔍 Resolving channel {dest_arg}…"
    )

    # Resolve entity
    try:
        dest_raw = int(dest_arg) if dest_arg.lstrip("-").isdigit() else dest_arg
        dest_entity = await client.get_entity(dest_raw)
    except Exception as e:
        await status_msg.edit_text(f"❌ Could not resolve channel: {e}")
        return

    dest_name = getattr(dest_entity, "title", str(dest_arg))

    await status_msg.edit_text(
        f"🔍 Scanning *{dest_name}* for duplicate files…\n"
        f"This may take a while for large channels.",
        parse_mode="Markdown",
    )

    # ── First pass: collect all messages, group by (filename, filesize) ────────
    # seen: key → oldest message ID
    # dups: list of message IDs to delete (all copies after the first)
    seen: dict = {}
    dups: list = []
    scanned = 0

    try:
        async for msg in client.iter_messages(dest_entity, reverse=True):
            scanned += 1
            f = getattr(msg, "file", None)
            if f and getattr(f, "name", None) and getattr(f, "size", None):
                key = (f.name, f.size)
                if key in seen:
                    dups.append(msg.id)  # newer copy — queue for deletion
                else:
                    seen[key] = msg.id   # oldest copy — keep it

            if scanned % 1000 == 0:
                try:
                    await status_msg.edit_text(
                        f"🔍 Scanning *{dest_name}*…\n"
                        f"Scanned: {scanned:,} messages | Duplicates: {len(dups):,}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        await status_msg.edit_text(
            f"⚠️ Scan cancelled after {scanned:,} messages.\n"
            f"Found {len(dups):,} duplicates so far — not deleted."
        )
        return
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Scan error after {scanned:,} messages: {e}"
        )
        return

    if not dups:
        await status_msg.edit_text(
            f"✅ No duplicates found in *{dest_name}*!\n"
            f"Scanned {scanned:,} messages — every file is unique.",
            parse_mode="Markdown",
        )
        return

    await status_msg.edit_text(
        f"🗑 Found *{len(dups):,}* duplicate messages in *{dest_name}*.\n"
        f"Scanned {scanned:,} total messages.\n\n"
        f"Deleting now…",
        parse_mode="Markdown",
    )

    # ── Second pass: delete duplicates in batches of 100 ──────────────────────
    deleted = 0
    failed_del = 0

    for i in range(0, len(dups), 100):
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

        # Progress update every 500 deletions
        if deleted % 500 < 100 or i + 100 >= len(dups):
            try:
                await status_msg.edit_text(
                    f"🗑 Deleting duplicates from *{dest_name}*…\n"
                    f"Deleted: {deleted:,} / {len(dups):,}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        await asyncio.sleep(0.5)  # avoid flood wait during bulk delete

    lines = [
        f"✅ *Deduplication complete* for *{dest_name}*!",
        "",
        f"📊 Scanned:  {scanned:,} messages",
        f"🗑 Duplicates found:  {len(dups):,}",
        f"✅ Deleted:  {deleted:,}",
    ]
    if failed_del:
        lines.append(f"❌ Failed to delete:  {failed_del:,}")

    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")


def get_purgedups_handlers():
    return [CommandHandler("purgedups", purgedups_cmd)]
