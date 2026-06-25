"""
/channelinfo <channel_id_or_username>

Shows message count, date range, and media breakdown for any channel
before you start a copy job. Requires the userbot to be connected.
If no argument is given and SOURCE_CHANNEL is set, uses that.
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import config
import userbot_bridge as bridge

logger = logging.getLogger(__name__)

try:
    from telethon.tl.types import (
        InputMessagesFilterVideo,
        InputMessagesFilterPhotos,
        InputMessagesFilterDocument,
        InputMessagesFilterMusic,
        InputMessagesFilterVoice,
        InputMessagesFilterGif,
    )
    _TELETHON_OK = True
except ImportError:
    _TELETHON_OK = False


def _fmt_date(dt) -> str:
    if dt is None:
        return "unknown"
    try:
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%b %d, %Y  %H:%M UTC")
    except Exception:
        return str(dt)


async def _count(client, entity, flt) -> int:
    """Return total message count for a given Telethon message filter."""
    try:
        result = await client.get_messages(entity, limit=0, filter=flt)
        return result.total
    except Exception:
        return 0


async def channelinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    # Resolve which channel to inspect
    if args:
        raw = args[0].strip()
        try:
            target = int(raw)
        except ValueError:
            target = raw          # @username
    elif config.SOURCE_CHANNEL:
        target = config.SOURCE_CHANNEL
        raw = str(target)
    else:
        await update.message.reply_text(
            "📊 *Channel Info*\n\n"
            "Usage: `/channelinfo <channel\\_id\\_or\\_username>`\n\n"
            "Examples:\n"
            "`/channelinfo -1001234567890`\n"
            "`/channelinfo @MyChannel`\n\n"
            "_Tip: run /channelinfo with no argument if you've already set a "
            "source channel with /setsource — it will use that one automatically._",
            parse_mode="Markdown",
        )
        return

    # Userbot must be connected to read message history
    if not bridge.is_ready(context.bot_data):
        await update.message.reply_text(
            "❌ *Userbot not connected.*\n\n"
            "Connect first with /login, then try again.",
            parse_mode="Markdown",
        )
        return

    client = bridge.get_client(context.bot_data)
    status = await update.message.reply_text(
        f"⏳ Fetching info for `{raw}`…", parse_mode="Markdown"
    )

    try:
        entity = await client.get_entity(target)

        # ── Total message count ───────────────────────────────────────────────
        total_result = await client.get_messages(entity, limit=0)
        total_msgs   = total_result.total

        # ── First and last message dates ──────────────────────────────────────
        first_list = await client.get_messages(entity, limit=1, reverse=True)
        last_list  = await client.get_messages(entity, limit=1)
        first_date = _fmt_date(first_list[0].date) if first_list else "unknown"
        last_date  = _fmt_date(last_list[0].date)  if last_list  else "unknown"

        # ── Media breakdown — all six queries fire in parallel ────────────────
        if _TELETHON_OK:
            videos, photos, docs, audio, voice, gifs = await asyncio.gather(
                _count(client, entity, InputMessagesFilterVideo()),
                _count(client, entity, InputMessagesFilterPhotos()),
                _count(client, entity, InputMessagesFilterDocument()),
                _count(client, entity, InputMessagesFilterMusic()),
                _count(client, entity, InputMessagesFilterVoice()),
                _count(client, entity, InputMessagesFilterGif()),
            )
        else:
            videos = photos = docs = audio = voice = gifs = 0

        # ── Channel metadata ──────────────────────────────────────────────────
        name     = getattr(entity, "title", None) or getattr(entity, "username", None) or str(target)
        username = getattr(entity, "username", None)
        members  = getattr(entity, "participants_count", None)

        username_line = f"\n🔗 `@{username}`" if username else ""
        members_line  = f"\n👥 Subscribers: `{members:,}`" if members else ""

        media_total = videos + photos + docs + audio + voice + gifs
        text_other  = max(0, total_msgs - media_total)

        # Size estimate (very rough — videos avg 200 MB, docs avg 50 MB)
        est_gb = (videos * 200 + docs * 50) / 1024
        size_line = f"\n📦 Rough size estimate: ~`{est_gb:.1f} GB`" if est_gb > 0.1 else ""

        text = (
            f"📊 *Channel Info*\n\n"
            f"📛 *{name}*{username_line}\n"
            f"🆔 `{target}`{members_line}\n\n"
            f"📬 *Total messages:* `{total_msgs:,}`\n"
            f"📅 *First message:* {first_date}\n"
            f"📅 *Last message:*  {last_date}"
            f"{size_line}\n\n"
            f"*Media breakdown:*\n"
            f"🎬 Videos    : `{videos:,}`\n"
            f"📷 Photos    : `{photos:,}`\n"
            f"📄 Documents : `{docs:,}`\n"
            f"🎵 Audio     : `{audio:,}`\n"
            f"🎤 Voice     : `{voice:,}`\n"
            f"🎞 GIFs      : `{gifs:,}`\n"
            f"💬 Text/Other: `{text_other:,}`\n\n"
            f"_Ready to copy? Use /copy and enter `{target}` as source._"
        )

        await status.edit_text(text, parse_mode="Markdown")

    except Exception as e:
        logger.exception("channelinfo error for %s", target)
        await status.edit_text(
            f"❌ Could not fetch info for `{raw}`.\n\n"
            f"*Reason:* `{e}`\n\n"
            f"Make sure:\n"
            f"• The channel ID / username is correct\n"
            f"• Your userbot is a member of that channel\n"
            f"• The userbot is connected (`/status`)",
            parse_mode="Markdown",
        )


def get_handlers():
    return [CommandHandler("channelinfo", channelinfo_cmd)]
