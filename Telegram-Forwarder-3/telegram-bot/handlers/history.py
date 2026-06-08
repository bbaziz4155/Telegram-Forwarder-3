import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from states import MAIN_MENU, FORWARD_HISTORY_SOURCE, FORWARD_HISTORY_DEST, FORWARD_HISTORY_LIMIT
import userbot_bridge as bridge
import config

logger = logging.getLogger(__name__)


async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not bridge.is_ready(context.bot_data):
        locked = bridge.is_locked(context.bot_data)
        msg = (
            "⏳ *Userbot is still connecting…*\n\nPlease wait a moment and try again."
            if locked else
            "❌ *Userbot not connected*\n\n"
            "Tap *🔑 Connect Userbot* in the menu (or send /login) to sign in\n"
            "with your phone number and OTP. Your session is saved permanently."
        )
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu")]])
        )
        return MAIN_MENU

    await query.edit_message_text(
        "📜 *Forward History*\n\n"
        "Copies recent messages from a source channel to a destination — "
        "without the 'Forwarded from' tag.\n\n"
        "Step 1/3: Send me the *source channel ID* (copy FROM).\n\n"
        "💡 Use /listchats to find channel IDs.",
        parse_mode="Markdown"
    )
    return FORWARD_HISTORY_SOURCE

async def history_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Please send a number (e.g. -1001234567890).")
        return FORWARD_HISTORY_SOURCE

    client = bridge.get_client(context.bot_data)
    try:
        entity = await client.get_entity(chat_id)
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)
    except Exception:
        chat_name = str(chat_id)

    context.user_data["hist_source_id"] = chat_id
    context.user_data["hist_source_name"] = chat_name

    await update.message.reply_text(
        f"✅ Source: *{chat_name}*\n\n"
        "Step 2/3: Send me the *destination channel ID* (copy TO).",
        parse_mode="Markdown"
    )
    return FORWARD_HISTORY_DEST


async def history_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Please send a number.")
        return FORWARD_HISTORY_DEST

    client = bridge.get_client(context.bot_data)
    try:
        entity = await client.get_entity(chat_id)
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)
    except Exception:
        chat_name = str(chat_id)

    context.user_data["hist_dest_id"] = chat_id
    context.user_data["hist_dest_name"] = chat_name

    await update.message.reply_text(
        f"✅ Destination: *{chat_name}*\n\n"
        "Step 3/3: How many recent messages to copy? (1–500)\n"
        "Send a number, e.g. `100`",
        parse_mode="Markdown"
    )
    return FORWARD_HISTORY_LIMIT


async def history_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        limit = int(text)
        if limit < 1 or limit > 500:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please send a number between 1 and 500.")
        return FORWARD_HISTORY_LIMIT

    source_id   = context.user_data["hist_source_id"]
    source_name = context.user_data["hist_source_name"]
    dest_id     = context.user_data["hist_dest_id"]
    dest_name   = context.user_data["hist_dest_name"]

    status_msg = await update.message.reply_text(
        f"⏳ Copying up to *{limit}* messages from *{source_name}* to *{dest_name}*…\n\n"
        "_This may take a while for large batches. The bot stays responsive while copying._",
        parse_mode="Markdown"
    )

    client = bridge.get_client(context.bot_data)
    bot    = context.application.bot
    chat_id    = update.message.chat_id
    msg_id     = status_msg.message_id

    async def _do_history_copy():
        from userbot.sender import _do_send, send_album
        from userbot.filter_utils import matches_filter
        caption_replacement = config.CAPTION_REPLACE

        copied  = 0
        skipped = 0
        failed  = 0

        try:
            source_entity = await client.get_entity(source_id)
            dest_entity   = await client.get_entity(dest_id)

            # Collect then reverse so messages are sent oldest → newest
            messages = []
            async for msg in client.iter_messages(source_entity, limit=limit, reverse=False):
                messages.append(msg)
            messages.reverse()

            album_buf: dict   = {}
            album_order: list = []

            async def _flush_album(gid):
                nonlocal copied, failed
                msgs_in = album_buf.pop(gid, [])
                if gid in album_order:
                    album_order.remove(gid)
                if not msgs_in:
                    return
                result = await send_album(client, dest_entity, msgs_in,
                                          caption_replacement=caption_replacement)
                if result == "ok":
                    copied += len(msgs_in)
                else:
                    failed += len(msgs_in)
                await asyncio.sleep(0.4)

            for msg in messages:
                gid = msg.grouped_id
                if gid:
                    if gid not in album_buf:
                        album_buf[gid] = []
                        album_order.append(gid)
                    album_buf[gid].append(msg)
                else:
                    for old_gid in list(album_order):
                        await _flush_album(old_gid)

                    if not matches_filter(msg, set(), skip_text=False):
                        skipped += 1
                        continue

                    result = await _do_send(client, dest_entity, msg,
                                            caption_replacement=caption_replacement)
                    if result == "ok":
                        copied += 1
                    elif result == "skip":
                        skipped += 1
                    else:
                        failed += 1
                    await asyncio.sleep(0.35)

            for gid in list(album_order):
                await _flush_album(gid)

        except asyncio.CancelledError:
            try:
                await bot.edit_message_text(
                    "⛔ History copy cancelled.",
                    chat_id=chat_id, message_id=msg_id,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
                    ),
                )
            except Exception:
                pass
            return

        except Exception as e:
            logger.exception("History copy error")
            try:
                await bot.edit_message_text(
                    f"❌ Error during history copy: {e}",
                    chat_id=chat_id, message_id=msg_id,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
                    ),
                )
            except Exception:
                pass
            return

        status_icon = "✅" if failed == 0 else "⚠️"
        try:
            await bot.edit_message_text(
                f"{status_icon} *History Copy Complete*\n\n"
                f"✅ Copied  : `{copied:,}`\n"
                f"⏭ Skipped : `{skipped:,}`\n"
                f"❌ Failed  : `{failed:,}`\n\n"
                f"📡 From: *{source_name}*\n"
                f"📥 To: *{dest_name}*",
                chat_id=chat_id, message_id=msg_id,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]]
                ),
            )
        except Exception:
            pass

    # Store reference so the task isn't garbage-collected before it finishes.
    task = asyncio.create_task(_do_history_copy())
    context.bot_data["_history_task"] = task
    return MAIN_MENU
