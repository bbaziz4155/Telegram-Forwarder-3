"""
/setsource <id>  — change the default source channel (copy FROM)
/setdest   <id>  — change the default destination channel (copy TO)
/channels        — show the current source and destination

Changes are saved to data/channel_settings.json and survive restarts.
"""
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import channel_settings
import config

logger = logging.getLogger(__name__)


async def _resolve_name(bot, chat_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or chat.username or str(chat_id)
    except Exception:
        return str(chat_id)


async def setsource_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    # No argument → show current value
    if not args:
        if config.SOURCE_CHANNEL:
            name = await _resolve_name(context.bot, config.SOURCE_CHANNEL)
            current = f"`{name}` (`{config.SOURCE_CHANNEL}`)"
        else:
            current = "_not set_"
        await update.message.reply_text(
            f"📡 *Current source channel:* {current}\n\n"
            "To change it, send the channel ID:\n"
            "`/setsource -1001234567890`",
            parse_mode="Markdown",
        )
        return

    try:
        new_id = int(args[0].strip())
    except ValueError:
        await update.message.reply_text(
            "❌ That doesn't look like a channel ID.\n\n"
            "Send a number, e.g.:\n`/setsource -1001957754060`",
            parse_mode="Markdown",
        )
        return

    config.SOURCE_CHANNEL = new_id
    channel_settings.save()

    name = await _resolve_name(context.bot, new_id)
    await update.message.reply_text(
        f"✅ *Source channel updated!*\n\n"
        f"📡 `{name}` (`{new_id}`)\n\n"
        f"All future /copy, /dryrun and /sync jobs will use this as the default source.",
        parse_mode="Markdown",
    )


async def setdest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    # No argument → show current value
    if not args:
        if config.DEST_CHANNEL:
            name = await _resolve_name(context.bot, config.DEST_CHANNEL)
            current = f"`{name}` (`{config.DEST_CHANNEL}`)"
        else:
            current = "_not set_"
        await update.message.reply_text(
            f"📥 *Current destination channel:* {current}\n\n"
            "To change it, send the channel ID:\n"
            "`/setdest -1003563437550`",
            parse_mode="Markdown",
        )
        return

    try:
        new_id = int(args[0].strip())
    except ValueError:
        await update.message.reply_text(
            "❌ That doesn't look like a channel ID.\n\n"
            "Send a number, e.g.:\n`/setdest -1003563437550`",
            parse_mode="Markdown",
        )
        return

    config.DEST_CHANNEL = new_id
    channel_settings.save()

    name = await _resolve_name(context.bot, new_id)
    await update.message.reply_text(
        f"✅ *Destination channel updated!*\n\n"
        f"📥 `{name}` (`{new_id}`)\n\n"
        f"All future /copy, /dryrun and /sync jobs will send files here by default.",
        parse_mode="Markdown",
    )


async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    src_id = config.SOURCE_CHANNEL
    dst_id = config.DEST_CHANNEL

    src_name = (await _resolve_name(context.bot, src_id)) if src_id else "not set"
    dst_name = (await _resolve_name(context.bot, dst_id)) if dst_id else "not set"

    src_line = f"`{src_name}` (`{src_id}`)" if src_id else "_not set_"
    dst_line = f"`{dst_name}` (`{dst_id}`)" if dst_id else "_not set_"

    await update.message.reply_text(
        f"📡 *Source channel:*\n{src_line}\n\n"
        f"📥 *Destination channel:*\n{dst_line}\n\n"
        f"To change:\n"
        f"`/setsource <channel\\_id>` — change source\n"
        f"`/setdest <channel\\_id>` — change destination",
        parse_mode="Markdown",
    )


def get_handlers():
    return [
        CommandHandler("setsource", setsource_cmd),
        CommandHandler("setdest",   setdest_cmd),
        CommandHandler("channels",  channels_cmd),
    ]
