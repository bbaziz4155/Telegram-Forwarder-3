"""
Bot handlers that drive the Telethon userbot from within the PTB Telegram bot:

  /copy     — guided bulk-copy wizard (no forwarded-from tag)
  /dryrun   — scan source and preview what would be copied
  /sync     — start live auto-sync (new messages forwarded instantly)
  /stopsync — stop the running auto-sync
  /status   — show current copy-job progress
  /stopjob  — cancel the running copy job
  /resume   — manually restart an interrupted copy job from its last checkpoint
"""
import asyncio
import json
import logging
import os
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import userbot_bridge as bridge
from userbot.forwarder import copy_channel_files, dry_run_results, list_chats
from userbot.sync import start_sync_handler
from userbot.filter_utils import parse_ext_filter
from userbot.notifier import ProgressNotifier
from userbot.checkpoint import CHECKPOINTS_DIR as _CHECKPOINTS_DIR
from states import COPY_AWAIT_SRC, COPY_AWAIT_DST, COPY_OPTIONS, COPY_AWAIT_REPLACE
from handlers import autoresume as _ar
import config

logger = logging.getLogger(__name__)

_FILTER_CYCLE = ["ALL", "mkv", "mp4", "mkv,mp4", "mkv,mp4,avi"]
_NOTIFY_CYCLE = [100, 200, 500, 0]
# (label, delay_seconds) — Safe is the default; avoids Telegram flood waits for large media
_SPEED_CYCLE  = [("🛡 Safe (2s)", 2.0), ("⏱ Steady (1s)", 1.0), ("🐢 Normal (0.35s)", 0.35), ("🚀 Fast (0.05s)", 0.05), ("⚡ Turbo (max)", 0.0)]
_SPEED_CB    = "speed_set:"
MIN_EDIT_INTERVAL = 5.0

# /resume inline-button callback IDs
_RESUME_CONFIRM = "resume_confirm"
_RESUME_CANCEL  = "resume_cancel"


# ═══════════════════════════════════════════════════════════════════════════════
#  BotProgressNotifier — edits a PTB bot message instead of Saved Messages
# ═══════════════════════════════════════════════════════════════════════════════

class BotProgressNotifier(ProgressNotifier):
    """Sends copy progress by editing a PTB bot message."""

    def __init__(self, bot, chat_id: int, message_id: int, every: int = 100,
                 bot_data: dict = None,
                 stats_key: str = "active_copy_stats",
                 flood_key: str = "active_flood_wait"):
        self.bot          = bot
        self.chat_id      = chat_id
        self.message_id   = message_id
        self.every        = every
        self.bot_data     = bot_data   # written on every tick so /status is always fresh
        self.stats_key    = stats_key
        self.flood_key    = flood_key
        self._last_notify = 0
        self._last_edit   = 0.0
        self._started     = time.time()
        self.client       = None  # not used — keeps ProgressNotifier API compat

    def _elapsed(self) -> float:
        return time.time() - self._started

    def _eta_str(self, copied: int, total: int) -> str:
        if copied == 0 or total == 0:
            return "?"
        rate = copied / max(self._elapsed(), 0.001)
        secs = int(max(0, total - copied) / rate)
        if secs < 60:
            return f"{secs}s"
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if m < 60 else f"{m // 60}h {m % 60}m"

    def _bar(self, copied: int, total: int) -> str:
        pct    = int(copied / total * 100) if total else 0
        filled = int(10 * pct / 100)
        return f"[{'█' * filled}{'░' * (10 - filled)}] {pct}%"

    async def tick(self, copied, skipped, failed, total, source_name="", dest_name=""):
        # Always update live stats so /status reads real numbers
        if self.bot_data is not None:
            self.bot_data[self.stats_key] = {
                "copied": copied, "skipped": skipped,
                "failed": failed, "total":  total,
            }
        if self.every <= 0 or copied == 0:
            return
        if copied - self._last_notify < self.every:
            return
        now = time.time()
        if now - self._last_edit < MIN_EDIT_INTERVAL:
            return
        self._last_notify = copied
        self._last_edit   = now
        m, s = divmod(int(self._elapsed()), 60)
        text = (
            f"📊 *Copy Progress*\n"
            f"`{self._bar(copied, total)}`\n\n"
            f"✅ Copied  : `{copied:,}` / `{total:,}`\n"
            f"⏭ Skipped : `{skipped:,}`\n"
            f"❌ Failed  : `{failed:,}`\n"
            f"⏱ Elapsed : `{m}m {s}s`\n"
            f"⏳ ETA     : `{self._eta_str(copied, total)}`\n\n"
            f"_/stopjob to cancel_"
        )
        if source_name:
            text += f"\n📡 `{source_name}` → `{dest_name}`"
        try:
            await self.bot.edit_message_text(
                text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug(f"BotNotifier tick: {e}")

    async def done(self, copied, skipped, failed, total, source_name="", dest_name=""):
        m, s  = divmod(int(self._elapsed()), 60)
        tag   = "✅ *Copy Complete!*" if failed == 0 else "⚠️ *Copy Finished (with errors)*"
        text  = (
            f"{tag}\n\n"
            f"✅ Sent      : `{copied:,}`\n"
            f"⏭ Skipped   : `{skipped:,}`\n"
            f"❌ Failed    : `{failed:,}`\n"
            f"⏱ Total time: `{m}m {s}s`\n"
        )
        if source_name:
            text += f"\n📡 `{source_name}` → `{dest_name}`"
        try:
            await self.bot.edit_message_text(
                text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await self.bot.send_message(self.chat_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"BotNotifier done failed: {e}")

    # Only send a Telegram message for flood waits longer than this threshold.
    # Short waits (< 60s) are silently absorbed — no spam on Normal/Fast speed.
    FLOOD_NOTIFY_THRESHOLD = 60

    async def flood_wait(self, seconds: int):
        # Always record in bot_data so /status shows a countdown regardless of size
        if self.bot_data is not None:
            self.bot_data[self.flood_key] = {
                "until":   time.time() + seconds,
                "seconds": seconds,
            }
        # Only ping the user for significant waits — small ones are absorbed silently
        if seconds < self.FLOOD_NOTIFY_THRESHOLD:
            logger.info("Flood wait %ds — absorbing silently (< %ds threshold)",
                        seconds, self.FLOOD_NOTIFY_THRESHOLD)
            return
        m, s = divmod(seconds, 60)
        wait_str = f"{m}m {s}s" if m > 0 else f"{s}s"
        try:
            await self.bot.send_message(
                self.chat_id,
                f"⏳ *Telegram rate limit hit!*\n\n"
                f"Pausing copy job for `{wait_str}` then resuming automatically.\n"
                f"_Use /status to see the countdown._",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"BotNotifier flood_wait notify failed: {e}")
    async def scan_progress(self, scanned: int, unique: int):
        """Update the bot message during destination pre-scan."""
        now = time.time()
        if now - self._last_edit < MIN_EDIT_INTERVAL:
            return
        self._last_edit = now
        if scanned == -1:
            # -1 = scan timed out
            try:
                await self.bot.edit_message_text(
                    f"⏱ *Pre-scan timed out* — starting copy with partial dedup.\n\n"
                    f"📦 Files found so far: `{unique:,}`",
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return
        try:
            await self.bot.edit_message_text(
                f"🔍 *Pre-scanning destination…*\n\n"
                f"Files found: `{scanned:,}` (unique: `{unique:,}`)\n\n"
                "_Checking what's already in the destination to prevent duplicates.\n"
                "Send /stopjob to skip and start immediately._",
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug(f"scan_progress edit failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_id(text: str):
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        return text


def _not_ready(locked: bool = False, starting_up: bool = False) -> str:
    if locked:
        return (
            "⏳ *Userbot session is busy.*\n\n"
            "Another process is holding the session file. "
            "Please wait a moment and try again, or restart the bot."
        )
    if starting_up:
        return (
            "⏳ *Userbot is starting up…*\n\n"
            "Your session is valid — the bot is reconnecting in the background.\n"
            "Please wait a few seconds and try again."
        )
    return (
        "❌ *Userbot not connected.*\n\n"
        "To enable /copy, /dryrun, and /sync:\n"
        "1. Tap *🔑 Connect Userbot* in /menu (or send /login)\n"
        "2. Enter your phone number and the OTP Telegram sends you\n"
        "3. Done — your session is saved and survives restarts"
    )


def _default_opts(mode: str) -> dict:
    return {
        "mode":                mode,
        "skip_text":           config.SKIP_TEXT,
        "filter_idx":          0,
        "filter_label":        "ALL",
        "allowed_exts":        set(config.ALLOWED_EXTS),
        "caption_replacement": config.CAPTION_REPLACE,
        "caption_suffix":      config.CAPTION_SUFFIX,
        "notify_every":        config.NOTIFY_EVERY if mode != "sync" else 0,
        "speed_idx":           0,                      # index into _SPEED_CYCLE
        "rate_delay":          _SPEED_CYCLE[0][1],     # seconds between sends
        "dual_copy":           False,                  # use two accounts in parallel
    }


def _opts_keyboard(opts: dict, dual_available: bool = False) -> InlineKeyboardMarkup:
    mode = opts["mode"]
    skip_lbl   = f"{'✅' if opts['skip_text'] else '📝'} Text posts: {'SKIP' if opts['skip_text'] else 'INCLUDE (season labels, hashtags)'}"
    filter_lbl = f"📁 File filter: {opts['filter_label']}"
    repl_lbl   = (f"✏️ @username → {opts['caption_replacement']}"
                 if opts["caption_replacement"]
                 else "✏️ @username → keep original")

    rows = [
        [InlineKeyboardButton(skip_lbl,   callback_data="copt_skip")],
        [InlineKeyboardButton(filter_lbl, callback_data="copt_filter")],
        [InlineKeyboardButton(repl_lbl,   callback_data="copt_replace")],
    ]

    if mode != "sync":
        n = opts["notify_every"]
        notify_lbl = f"🔔 Notify every {n} files" if n else "🔕 Notifications OFF"
        rows.append([InlineKeyboardButton(notify_lbl, callback_data="copt_notify")])

        speed_lbl = _SPEED_CYCLE[opts.get("speed_idx", 0)][0]
        rows.append([InlineKeyboardButton(f"⏩ Speed: {speed_lbl}", callback_data="copt_speed")])

        if dual_available or opts.get("dual_copy"):
            _dual_on = opts.get("dual_copy")
            dual_lbl = ("✅ Dual Copy: ON" if _dual_on else "🔀 Dual Copy: OFF")
            rows.append([InlineKeyboardButton(dual_lbl, callback_data="copt_dual")])

    start_labels = {
        "copy":   "▶ Start Copy",
        "dryrun": "🔍 Start Dry Run",
        "sync":   "🔄 Start Sync",
    }
    rows.append([
        InlineKeyboardButton(start_labels[mode], callback_data="copt_start"),
        InlineKeyboardButton("❌ Cancel",         callback_data="copt_cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _opts_text(src_raw: str, dst_raw: str, opts: dict) -> str:
    labels = {"copy": "Copy Files", "dryrun": "Dry Run", "sync": "Auto-Sync"}
    return (
        f"⚙️ *{labels[opts['mode']]} Settings*\n\n"
        f"📡 Source: `{src_raw}`\n"
        f"📥 Dest:   `{dst_raw}`\n\n"
        f"Tap to change options, then tap *Start*:"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Conversation — entry points  (/copy, /dryrun, /sync)
# ═══════════════════════════════════════════════════════════════════════════════

async def copy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _start_wizard(update, context, "copy")

async def dryrun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _start_wizard(update, context, "dryrun")

async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _start_wizard(update, context, "sync")


async def _start_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    if not bridge.is_ready(context.bot_data):
        locked = bridge.is_locked(context.bot_data)
        # If the client object exists, the session is valid but still reconnecting
        starting_up = (not locked) and (bridge.get_client(context.bot_data) is not None)
        await update.message.reply_text(
            _not_ready(locked, starting_up), parse_mode="Markdown"
        )
        return ConversationHandler.END

    if mode in ("copy", "dryrun"):
        task = context.bot_data.get("active_copy_task")
        if task and not task.done():
            await update.message.reply_text(
                "⚠️ A copy job is already running.\n"
                "Use /status to see progress or /stopjob to cancel it."
            )
            return ConversationHandler.END

    if mode == "sync" and context.bot_data.get("active_sync_handler"):
        await update.message.reply_text(
            "⚠️ Auto-sync is already running. Use /stopsync to stop it."
        )
        return ConversationHandler.END

    context.user_data["copy_mode"] = mode
    context.user_data["copy_opts"] = _default_opts(mode)

    # If config.py has both channels set, skip the input steps entirely
    if config.SOURCE_CHANNEL and config.DEST_CHANNEL:
        context.user_data["copy_src"]      = config.SOURCE_CHANNEL
        context.user_data["copy_src_raw"]  = str(config.SOURCE_CHANNEL)
        context.user_data["copy_dst"]      = config.DEST_CHANNEL
        context.user_data["copy_dst_raw"]  = str(config.DEST_CHANNEL)
        context.user_data["copy_dsts"]     = [config.DEST_CHANNEL]
        context.user_data["copy_dsts_raw"] = [str(config.DEST_CHANNEL)]
        opts = context.user_data["copy_opts"]
        mode_labels = {"copy": "Copy Files", "dryrun": "Dry Run", "sync": "Auto-Sync"}
        msg = await update.message.reply_text(
            f"⚙️ *{mode_labels[mode]}*\n\n"
            f"📡 Source: `{config.SOURCE_CHANNEL}`\n"
            f"📥 Dest:   `{config.DEST_CHANNEL}`\n\n"
            f"_(Defaults from config.py — tap to change options, then Start)_",
            parse_mode="Markdown",
            reply_markup=_opts_keyboard(opts),
        )
        context.user_data["opts_msg_id"] = msg.message_id
        return COPY_OPTIONS

    labels = {
        "copy":   "📦 *Copy Files* — sends without 'Forwarded from' tag",
        "dryrun": "🔍 *Dry Run* — preview only, nothing is sent",
        "sync":   "🔄 *Auto-Sync* — forward new messages instantly",
    }

    # Source already configured: pre-fill it and jump straight to the dest prompt
    if config.SOURCE_CHANNEL:
        context.user_data["copy_src"]     = config.SOURCE_CHANNEL
        context.user_data["copy_src_raw"] = str(config.SOURCE_CHANNEL)
        await update.message.reply_text(
            f"{labels[mode]}\n\n"
            f"📡 Source: `{config.SOURCE_CHANNEL}` ✓\n\n"
            "📥 Enter the *destination* channel ID or @username:\n"
            "_(e.g. `-1001234567890`)_",
            parse_mode="Markdown",
        )
        return COPY_AWAIT_DST

    await update.message.reply_text(
        f"{labels[mode]}\n\n"
        f"Enter the *source* channel ID or @username:\n"
        f"_(e.g. `-1001234567890`)_",
        parse_mode="Markdown",
    )
    return COPY_AWAIT_SRC


# ── Step 1: source ────────────────────────────────────────────────────────────

async def got_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["copy_src"]     = _parse_id(text)
    context.user_data["copy_src_raw"] = text
    mode = context.user_data.get("copy_mode", "copy")
    if mode == "sync":
        await update.message.reply_text(
            "📥 Enter the *destination* channel ID or @username:\n"
            "_(e.g. `-1001234567890`)_",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "📥 Enter the *destination* channel ID or @username:\n"
            "_(e.g. `-1001234567890`)_\n\n"
            "💡 *Multiple destinations?* Separate IDs with commas:\n"
            "_(e.g. `-1001234567890, -1009876543210, -1005555555555`)_",
            parse_mode="Markdown",
        )
    return COPY_AWAIT_DST


# ── Step 2: dest → show options keyboard ─────────────────────────────────────

async def got_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Parse comma-separated destinations (single dest still works fine)
    raw_parts = [p.strip() for p in text.split(",") if p.strip()]
    dsts      = [_parse_id(p) for p in raw_parts]

    context.user_data["copy_dsts"]     = dsts
    context.user_data["copy_dsts_raw"] = raw_parts
    # Keep legacy single keys pointing at the first dest for backward compat
    context.user_data["copy_dst"]     = dsts[0]
    context.user_data["copy_dst_raw"] = ", ".join(raw_parts)

    opts    = context.user_data["copy_opts"]
    src_raw = context.user_data["copy_src_raw"]
    dst_raw = context.user_data["copy_dst_raw"]

    msg = await update.message.reply_text(
        _opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_opts_keyboard(opts),
    )
    context.user_data["opts_msg_id"] = msg.message_id
    return COPY_OPTIONS


# ── Step 3: options keyboard interactions ─────────────────────────────────────

async def options_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data    = query.data
    opts    = context.user_data.get("copy_opts")
    if opts is None:
        # Conversation state expired — guide the user to restart
        await query.edit_message_text(
            "⚠️ Session expired. Please use /copy, /dryrun, or /sync to start again."
        )
        return ConversationHandler.END
    src_raw = context.user_data.get("copy_src_raw", "?")
    dst_raw = context.user_data.get("copy_dst_raw", "?")

    if data == "copt_cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    if data == "copt_skip":
        opts["skip_text"] = not opts["skip_text"]

    elif data == "copt_filter":
        opts["filter_idx"]   = (opts["filter_idx"] + 1) % len(_FILTER_CYCLE)
        opts["filter_label"] = _FILTER_CYCLE[opts["filter_idx"]]
        raw = opts["filter_label"]
        opts["allowed_exts"] = parse_ext_filter(raw) if raw != "ALL" else set()

    elif data == "copt_replace":
        cur = opts.get("caption_replacement", "")
        cur_display = cur if cur else "off"
        await query.edit_message_text(
            f"✏️ *Username Replacement*\n\n"
            f"Currently: `{cur_display}`\n\n"
            f"Any `@username` found in captions will be swapped to your username.\n\n"
            f"Send your replacement (e.g. `@backupchannek`), or send `off` to disable.",
            parse_mode="Markdown",
        )
        return COPY_AWAIT_REPLACE

    elif data == "copt_notify":
        cur = opts["notify_every"]
        try:
            idx = _NOTIFY_CYCLE.index(cur)
        except ValueError:
            idx = 0
        opts["notify_every"] = _NOTIFY_CYCLE[(idx + 1) % len(_NOTIFY_CYCLE)]

    elif data == "copt_speed":
        idx = (opts.get("speed_idx", 0) + 1) % len(_SPEED_CYCLE)
        opts["speed_idx"]  = idx
        opts["rate_delay"] = _SPEED_CYCLE[idx][1]

    elif data == "copt_dual":
        opts["dual_copy"] = not opts.get("dual_copy", False)

    elif data == "copt_start":
        await _launch_job(query, context, opts, src_raw, dst_raw)
        return ConversationHandler.END

    _dual_avail = bridge.is_ready_2(context.bot_data)
    await query.edit_message_text(
        _opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_opts_keyboard(opts, dual_available=_dual_avail),
    )
    return COPY_OPTIONS


# ═══════════════════════════════════════════════════════════════════════════════
#  Job launcher
# ═══════════════════════════════════════════════════════════════════════════════

async def _launch_job(query, context: ContextTypes.DEFAULT_TYPE, opts: dict, src_raw: str, dst_raw: str):
    mode    = opts["mode"]
    chat_id = query.message.chat_id
    bot     = context.application.bot
    client  = bridge.get_client(context.bot_data)
    src     = context.user_data["copy_src"]
    dst     = context.user_data["copy_dst"]   # first / only dest

    # Multi-destination list (falls back to single-dest for legacy paths)
    dsts     = context.user_data.get("copy_dsts",     [dst])
    dsts_raw = context.user_data.get("copy_dsts_raw", [context.user_data.get("copy_dst_raw", dst_raw)])

    mode_tags = {"copy": "▶ Copy", "dryrun": "🔍 Dry Run", "sync": "🔄 Auto-Sync"}

    if mode == "sync":
        # Sync only supports a single destination (event-handler based)
        if len(dsts) > 1:
            await query.edit_message_text(
                "⚠️ *Auto-Sync supports only one destination.*\n\n"
                "Please use /sync again and enter a single channel ID.",
                parse_mode="Markdown",
            )
            return
        await query.edit_message_text(
            f"🔄 Auto-Sync started!\n\n"
            f"📡 `{src_raw}` → `{dst_raw}`\n"
            f"_Forwarding new messages instantly…_",
            parse_mode="Markdown",
        )
        task = asyncio.create_task(
            _run_sync(client, src, dst, opts, bot, chat_id, context.bot_data)
        )
        context.bot_data["active_sync_task"] = task
        context.bot_data["active_sync_opts"] = opts
        context.bot_data["active_sync_src"]  = src
        context.bot_data["active_sync_dst"]  = dst
        return

    # ── Copy / Dry-run — supports multiple destinations ───────────────────────
    n = len(dsts)
    if n == 1:
        header = (
            f"{mode_tags[mode]} started!\n\n"
            f"📡 `{src_raw}` → `{dst_raw}`\n"
            f"_Progress updates will appear below…_"
        )
    else:
        dest_lines = "\n".join(f"  {i+1}. `{r}`" for i, r in enumerate(dsts_raw))
        header = (
            f"{mode_tags[mode]} started — *{n} destinations*\n\n"
            f"📡 `{src_raw}` →\n{dest_lines}\n\n"
            f"_Running destinations one by one…_"
        )
    await query.edit_message_text(header, parse_mode="Markdown")

    if mode == "dryrun":
        status_msg = await bot.send_message(chat_id, "⏳ Scanning channel…")
        task = asyncio.create_task(
            _run_multi_dryrun(client, src, dsts, dsts_raw, opts, bot, chat_id,
                              status_msg.message_id, context.bot_data)
        )
        context.bot_data["active_copy_task"] = task

    elif mode == "copy":
        opts["caption_suffix"] = context.user_data.get("caption_suffix", config.CAPTION_SUFFIX)
        _ar.save_resume(chat_id, src, dsts[0], opts)
        if opts.get("dual_copy") and bridge.is_ready_2(context.bot_data) and len(dsts) == 1:
            client_2 = bridge.get_client_2(context.bot_data)
            await query.edit_message_text(
                f"🔀 *Dual Copy started!*\n\n"
                f"📡 `{src_raw}` → `{dst_raw}`\n\n"
                f"_Two accounts copying in parallel — progress below…_",
                parse_mode="Markdown",
            )
            status_msg = await bot.send_message(chat_id, "⏳ Initializing dual copy…")
            task = asyncio.create_task(
                _run_dual_copy(client, client_2, src, dsts[0], opts, bot, chat_id,
                               status_msg.message_id, context.bot_data)
            )
        else:
            status_msg = await bot.send_message(chat_id, "⏳ Initializing copy…")
            task = asyncio.create_task(
                _run_multi_copy(client, src, dsts, dsts_raw, opts, bot, chat_id,
                                status_msg.message_id, context.bot_data)
            )
        context.bot_data["active_copy_task"]  = task
        context.bot_data["active_status_msg"] = (chat_id, status_msg.message_id)


# ── Background coroutines ─────────────────────────────────────────────────────


# ── Multi-destination dry-run ──────────────────────────────────────────────────

async def _run_multi_dryrun(client, src, dsts, dsts_raw, opts, bot, chat_id, msg_id, bot_data):
    """Run dry-run sequentially for each destination and report combined results."""
    try:
        all_results = []
        total = len(dsts)
        for i, (dst, dst_raw) in enumerate(zip(dsts, dsts_raw)):
            prefix = f"({i + 1}/{total}) " if total > 1 else ""
            try:
                await bot.edit_message_text(
                    f"⏳ {prefix}Scanning → `{dst_raw}`…",
                    chat_id=chat_id, message_id=msg_id,
                )
            except Exception:
                pass
            result = await dry_run_results(
                client, src, dst,
                allowed_exts=opts["allowed_exts"],
                caption_replacement=opts["caption_replacement"],
                skip_text=opts["skip_text"],
            )
            all_results.append((dst_raw, result))

        filt_note = f"\n🔎 Filter: `{opts['filter_label'].upper()}` only" if opts["allowed_exts"] else ""
        skip_note = "\n🚫 Text-only messages: *SKIPPED*" if opts["skip_text"] else ""

        if total == 1:
            dst_raw, result = all_results[0]
            if result is None:
                text = "❌ Could not resolve channels. Check the IDs and try again."
            else:
                text = (
                    f"🔍 *Dry Run Results*{filt_note}{skip_note}\n\n"
                    f"📡 `{result['source_name']}`\n"
                    f"📥 `{result['dest_name']}`\n\n"
                    f"📎 Single media : `{result['media']:,}`\n"
                    f"🖼 Albums       : `{result['albums']:,}`\n"
                    f"💬 Text msgs    : `{result['text']:,}`\n"
                    f"⏭ Filtered out : `{result['filtered']:,}`\n"
                    f"🗑 Empty/svc    : `{result['empty']:,}`\n\n"
                    f"*✅ Would copy: `{result['total_to_copy']:,}`* of `{result['total_scanned']:,}` scanned"
                )
        else:
            lines = [f"🔍 *Dry Run — {total} Destinations*{filt_note}{skip_note}\n"]
            grand_total = 0
            for i, (dst_raw, result) in enumerate(all_results):
                if result is None:
                    lines.append(f"{i + 1}\. `{dst_raw}` — ❌ Could not resolve")
                else:
                    tc = result["total_to_copy"]
                    ts = result["total_scanned"]
                    lines.append(
                        f"{i + 1}\. `{result['dest_name']}`\n"
                        f"   Would copy: `{tc:,}` / `{ts:,}` scanned"
                    )
                    grand_total += tc
            lines.append(f"\n*✅ Grand total: `{grand_total:,}` files would be copied*")
            text = "\n".join(lines)

        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown"
        )

    except asyncio.CancelledError:
        try:
            await bot.edit_message_text("⛔ Dry run cancelled.", chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    except Exception as e:
        logger.exception("Multi dry-run error")
        try:
            await bot.edit_message_text(f"❌ Error: {e}", chat_id=chat_id, message_id=msg_id)
        except Exception:
            await bot.send_message(chat_id, f"❌ Dry run error: {e}")
    finally:
        bot_data["active_copy_task"] = None


# ── Multi-destination copy ─────────────────────────────────────────────────────

async def _run_multi_copy(client, src, dsts, dsts_raw, opts, bot, chat_id, msg_id, bot_data):
    """Copy from one source to multiple destinations sequentially."""
    bot_data["active_copy_delay"] = opts.get("rate_delay", _SPEED_CYCLE[0][1])
    total       = len(dsts)
    all_stats   = []   # list of (dst_raw, stats_dict, error_str_or_None)
    cancelled   = False

    for i, (dst, dst_raw) in enumerate(zip(dsts, dsts_raw)):
        prefix = f"({i + 1}/{total}) " if total > 1 else ""

        # For 2+ destinations, send a fresh status message per destination
        if total > 1:
            try:
                await bot.edit_message_text(
                    f"📦 {prefix}Starting copy → `{dst_raw}`…",
                    chat_id=chat_id, message_id=msg_id,
                )
            except Exception:
                pass
            prog_msg  = await bot.send_message(chat_id, f"⏳ {prefix}Initialising…")
            notifier  = BotProgressNotifier(
                bot, chat_id, prog_msg.message_id,
                every=opts["notify_every"],
                bot_data=bot_data,
            )
        else:
            notifier = BotProgressNotifier(
                bot, chat_id, msg_id,
                every=opts["notify_every"],
                bot_data=bot_data,
            )

        try:
            await copy_channel_files(
                client, src, dst,
                allowed_exts=opts["allowed_exts"],
                caption_replacement=opts["caption_replacement"],
                caption_suffix=opts.get("caption_suffix", ""),
                notify_every=opts["notify_every"],
                skip_text=opts["skip_text"],
                notifier=notifier,
                interactive=False,
                rate_delay=lambda: bot_data.get(
                    "active_copy_delay", opts.get("rate_delay", _SPEED_CYCLE[0][1])
                ),
            )
            job_stats = bot_data.get("active_copy_stats", {})
            all_stats.append((dst_raw, job_stats.copy(), None))

        except asyncio.CancelledError:
            cancelled   = True
            job_stats   = bot_data.get("active_copy_stats", {})
            c = job_stats.get("copied",  0)
            s = job_stats.get("skipped", 0)
            f = job_stats.get("failed",  0)
            cancel_text = (
                f"⛔ *Copy Cancelled* {prefix}\n\n"
                f"✅ Copied: `{c:,}`  ⏭ Skipped: `{s:,}`  ❌ Failed: `{f:,}`\n\n"
                f"_Use /resume to continue from this point._"
            )
            try:
                await bot.edit_message_text(
                    cancel_text,
                    chat_id=chat_id,
                    message_id=notifier.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                try:
                    await bot.send_message(chat_id, cancel_text, parse_mode="Markdown")
                except Exception:
                    pass
            break  # stop remaining destinations

        except Exception as e:
            logger.exception("Copy error for dest %s", dst_raw)
            all_stats.append((dst_raw, {}, str(e)))
            # Edit the frozen progress message so the user sees the error clearly
            try:
                await bot.edit_message_text(
                    f"❌ *Copy error*\n\n`{str(e)[:300]}`\n\n"
                    f"_Make sure the userbot is a member of both channels._",
                    chat_id=chat_id,
                    message_id=notifier.message_id,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            try:
                await bot.send_message(
                    chat_id,
                    f"❌ Copy to `{dst_raw}` failed: {e}\n_Continuing with next destination…_",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            # Continue with next destination — don't break
    # ── Summary for multi-destination jobs ─────────────────────────────────────
    if total > 1 and all_stats and not cancelled:
        lines = ["📊 *Multi-Destination Copy Complete!*\n"]
        grand_copied = grand_skipped = grand_failed = 0
        for dst_raw, stats, err in all_stats:
            c = stats.get("copied",  0)
            s = stats.get("skipped", 0)
            f = stats.get("failed",  0)
            grand_copied  += c
            grand_skipped += s
            grand_failed  += f
            status_icon = "❌" if err else "✅"
            if err:
                lines.append(f"{status_icon} `{dst_raw[:40]}` — Error: _{err[:60]}_")
            else:
                lines.append(
                    f"{status_icon} `{dst_raw[:40]}`\n"
                    f"   ✅ {c:,} copied · ⏭ {s:,} skipped · ❌ {f:,} failed"
                )
        lines.append(
            f"\n*Grand total: ✅ {grand_copied:,} copied · "
            f"⏭ {grand_skipped:,} skipped · ❌ {grand_failed:,} failed*"
        )
        try:
            await bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")
        except Exception:
            pass

    # ── Cleanup ────────────────────────────────────────────────────────────────
    if not cancelled:
        _ar.clear_resume()
    bot_data["active_copy_task"]  = None
    bot_data["active_status_msg"] = None
    bot_data.pop("active_flood_wait", None)
    bot_data.pop("active_copy_stats", None)
    bot_data.pop("active_copy_delay", None)



# ── Dual-account parallel copy ─────────────────────────────────────────────────

async def _run_dual_copy(client, client_2, src, dst, opts, bot, chat_id, status_msg_id, bot_data):
    """
    Run two parallel copy workers — client handles the newer half of messages,
    client_2 handles the older half.  Both share the same SQLite dedup table.
    Progress is shown as two rows in the status message.
    """
    bot_data["active_copy_delay"] = opts.get("rate_delay", _SPEED_CYCLE[0][1])

    # ── Determine split point: find highest message ID in source ──────────────
    try:
        source_entity = await client.get_entity(src)
        newest = await client.get_messages(source_entity, limit=1)
        if not newest:
            raise ValueError("Source channel is empty")
        max_msg_id = newest[0].id
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("Dual copy: could not read source channel")
        try:
            await bot.edit_message_text(
                f"❌ *Dual copy failed*\n\n`{str(e)[:200]}`",
                chat_id=chat_id, message_id=status_msg_id, parse_mode="Markdown",
            )
        except Exception:
            pass
        bot_data["active_copy_task"]  = None
        bot_data["active_status_msg"] = None
        return

    mid_id = max_msg_id // 2
    logger.info("Dual copy: max_msg_id=%d split at mid_id=%d", max_msg_id, mid_id)

    # ── Initialise per-worker stats ───────────────────────────────────────────
    bot_data["active_copy_stats"]   = {"copied": 0, "skipped": 0, "failed": 0, "total": 0}
    bot_data["active_copy_stats_b"] = {"copied": 0, "skipped": 0, "failed": 0, "total": 0}

    try:
        await bot.edit_message_text(
            f"🔀 *Dual Copy initialising…*\n\n"
            f"📡 Split point: msg ID `{mid_id:,}`\n\n"
            f"👤 *Account A* (newer msgs): ⏳\n"
            f"👤 *Account B* (older msgs): ⏳\n\n"
            f"_/stopjob to cancel both_",
            chat_id=chat_id, message_id=status_msg_id, parse_mode="Markdown",
        )
    except Exception:
        pass

    # Notifiers: every=0 so they only update bot_data; status loop handles edits
    rate_delay = lambda: bot_data.get("active_copy_delay", opts.get("rate_delay", _SPEED_CYCLE[0][1]))

    notifier_a = BotProgressNotifier(
        bot, chat_id, status_msg_id,
        every=0,
        bot_data=bot_data,
        stats_key="active_copy_stats",
        flood_key="active_flood_wait",
    )
    notifier_b = BotProgressNotifier(
        bot, chat_id, status_msg_id,
        every=0,
        bot_data=bot_data,
        stats_key="active_copy_stats_b",
        flood_key="active_flood_wait_b",
    )

    # ── Status-update loop: edits the message with two-row progress ───────────
    async def _status_loop():
        while True:
            await asyncio.sleep(10)
            sa = bot_data.get("active_copy_stats",   {})
            sb = bot_data.get("active_copy_stats_b", {})
            fa = bot_data.get("active_flood_wait")
            fb = bot_data.get("active_flood_wait_b")

            def _row(label, stats, flood):
                co = stats.get("copied",  0)
                sk = stats.get("skipped", 0)
                fl_ = stats.get("failed",  0)
                to = stats.get("total",   0)
                tot = f"/{to:,}" if to else ""
                fw = ""
                if flood:
                    rem = int(flood["until"] - time.time())
                    if rem > 0:
                        fm, fs_ = divmod(rem, 60)
                        fw = f" ⏳{fm}m{fs_:02d}s" if fm else f" ⏳{rem}s"
                return (
                    f"👤 *{label}:* "
                    f"✅`{co:,}`{tot} ⏭`{sk:,}` ❌`{fl_:,}`{fw}"
                )

            lines = [
                "🔀 *Dual Copy running*\n",
                _row("Account A (newer)", sa, fa),
                _row("Account B (older)", sb, fb),
                "",
                "_/stopjob to cancel both_",
            ]
            try:
                await bot.edit_message_text(
                    "\n".join(lines),
                    chat_id=chat_id, message_id=status_msg_id, parse_mode="Markdown",
                )
            except Exception:
                pass

    # ── Spin up both worker tasks ─────────────────────────────────────────────
    async def _worker_a():
        await copy_channel_files(
            client, src, dst,
            allowed_exts=opts["allowed_exts"],
            caption_replacement=opts["caption_replacement"],
            caption_suffix=opts.get("caption_suffix", ""),
            notify_every=0,
            skip_text=opts["skip_text"],
            notifier=notifier_a,
            interactive=False,
            rate_delay=rate_delay,
            min_id=mid_id,
        )

    async def _worker_b():
        await copy_channel_files(
            client_2, src, dst,
            allowed_exts=opts["allowed_exts"],
            caption_replacement=opts["caption_replacement"],
            caption_suffix=opts.get("caption_suffix", ""),
            notify_every=0,
            skip_text=opts["skip_text"],
            notifier=notifier_b,
            interactive=False,
            rate_delay=rate_delay,
            max_id=mid_id + 1,
        )

    task_a      = asyncio.create_task(_worker_a())
    task_b      = asyncio.create_task(_worker_b())
    status_task = asyncio.create_task(_status_loop())

    cancelled = False
    try:
        await asyncio.gather(task_a, task_b)

    except asyncio.CancelledError:
        cancelled = True
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

    except Exception as e:
        logger.exception("Dual copy error")
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

    finally:
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)

        sa = bot_data.get("active_copy_stats",   {})
        sb = bot_data.get("active_copy_stats_b", {})
        ca, sa_, fa_ = sa.get("copied",0), sa.get("skipped",0), sa.get("failed",0)
        cb, sb_, fb_ = sb.get("copied",0), sb.get("skipped",0), sb.get("failed",0)
        tc, ts, tf = ca + cb, sa_ + sb_, fa_ + fb_

        if cancelled:
            _ar.clear_resume()
            text = (
                f"⛔ *Dual Copy Cancelled*\n\n"
                f"👤 *Account A:* ✅`{ca:,}` ⏭`{sa_:,}` ❌`{fa_:,}`\n"
                f"👤 *Account B:* ✅`{cb:,}` ⏭`{sb_:,}` ❌`{fb_:,}`\n\n"
                f"_Use /copy to start a new job._"
            )
        else:
            _ar.clear_resume()
            tag = "✅ *Dual Copy Complete!*" if tf == 0 else "⚠️ *Dual Copy Finished (with errors)*"
            text = (
                f"{tag}\n\n"
                f"👤 *Account A:* ✅`{ca:,}` ⏭`{sa_:,}` ❌`{fa_:,}`\n"
                f"👤 *Account B:* ✅`{cb:,}` ⏭`{sb_:,}` ❌`{fb_:,}`\n\n"
                f"*Total: ✅`{tc:,}` ⏭`{ts:,}` ❌`{tf:,}`*"
            )

        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=status_msg_id, parse_mode="Markdown",
            )
        except Exception:
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown")
            except Exception:
                pass

        bot_data["active_copy_task"]  = None
        bot_data["active_status_msg"] = None
        bot_data.pop("active_flood_wait",   None)
        bot_data.pop("active_flood_wait_b", None)
        bot_data.pop("active_copy_stats",   None)
        bot_data.pop("active_copy_stats_b", None)
        bot_data.pop("active_copy_delay",   None)


async def _run_dryrun(client, src, dst, opts, bot, chat_id, msg_id, bot_data):
    try:
        result = await dry_run_results(
            client, src, dst,
            allowed_exts=opts["allowed_exts"],
            caption_replacement=opts["caption_replacement"],
            skip_text=opts["skip_text"],
        )
        if result is None:
            await bot.edit_message_text(
                "❌ Could not resolve channels. Check the IDs and try again.",
                chat_id=chat_id,
                message_id=msg_id,
            )
            return
        filt_note = ""
        if opts["allowed_exts"]:
            filt_note = f"\n🔎 Filter: `{opts['filter_label'].upper()}` only"
        skip_note = "\n🚫 Text-only messages: *SKIPPED*" if opts["skip_text"] else ""
        text = (
            f"🔍 *Dry Run Results*{filt_note}{skip_note}\n\n"
            f"📡 `{result['source_name']}`\n"
            f"📥 `{result['dest_name']}`\n\n"
            f"📎 Single media : `{result['media']:,}`\n"
            f"🖼 Albums       : `{result['albums']:,}`\n"
            f"💬 Text msgs    : `{result['text']:,}`\n"
            f"⏭ Filtered out : `{result['filtered']:,}`\n"
            f"🗑 Empty/svc    : `{result['empty']:,}`\n\n"
            f"*✅ Would copy: `{result['total_to_copy']:,}`* of `{result['total_scanned']:,}` scanned"
        )
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown"
        )
    except asyncio.CancelledError:
        try:
            await bot.edit_message_text(
                "⛔ Dry run cancelled.", chat_id=chat_id, message_id=msg_id
            )
        except Exception:
            pass
    except Exception as e:
        logger.exception("Dry run error")
        try:
            await bot.edit_message_text(
                f"❌ Error: {e}", chat_id=chat_id, message_id=msg_id
            )
        except Exception:
            await bot.send_message(chat_id, f"❌ Dry run error: {e}")
    finally:
        # Always clear the task so /dryrun can be run again immediately
        bot_data["active_copy_task"] = None


async def _run_copy(client, src, dst, opts, notifier, bot, chat_id, bot_data):
    # Seed live-adjustable delay so /speed can read & update it mid-job
    bot_data["active_copy_delay"] = opts.get("rate_delay", _SPEED_CYCLE[0][1])
    _clear_resume = True   # set False on unexpected exception → keep file for auto-resume
    try:
        await copy_channel_files(
            client, src, dst,
            allowed_exts=opts["allowed_exts"],
            caption_replacement=opts["caption_replacement"],
            caption_suffix=opts.get("caption_suffix", ""),
            notify_every=opts["notify_every"],
            skip_text=opts["skip_text"],
            notifier=notifier,
            interactive=False,
            rate_delay=lambda: bot_data.get("active_copy_delay", opts.get("rate_delay", _SPEED_CYCLE[0][1])),
        )
    except asyncio.CancelledError:
        # Edit the progress message to a clear "cancelled" state — do NOT call
        # notifier.done() here because that shows "✅ Copy Complete!" which is
        # misleading when the job was actually stopped by the user.
        stats = bot_data.get("active_copy_stats", {})
        c = stats.get("copied",  0)
        s = stats.get("skipped", 0)
        f = stats.get("failed",  0)
        cancel_text = (
            f"⛔ *Copy Job Cancelled*\n\n"
            f"✅ Copied  : `{c:,}`\n"
            f"⏭ Skipped : `{s:,}`\n"
            f"❌ Failed  : `{f:,}`\n\n"
            f"_Use /resume to continue from this point._"
        )
        try:
            await bot.edit_message_text(
                cancel_text,
                chat_id=chat_id,
                message_id=notifier.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await bot.send_message(chat_id, cancel_text, parse_mode="Markdown")
            except Exception:
                pass
    except Exception as e:
        _clear_resume = False  # crash/error — preserve resume file for auto-resume on next start
        logger.exception("Copy job error")
        # Edit the progress message to show the error — do NOT call notifier.done()
        # because that shows "⚠️ Copy Finished (with errors)" which is misleading
        # when the job actually crashed mid-run.
        try:
            await bot.edit_message_text(
                f"❌ *Copy job crashed*\n\n"
                f"`{str(e)[:300]}`\n\n"
                f"_The job was interrupted. Use /resume to retry, or /copy to start fresh._",
                chat_id=chat_id,
                message_id=notifier.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await bot.send_message(chat_id, f"❌ Copy error: {e}")
            except Exception:
                pass
    finally:
        # Clear resume file on success or user /stopjob cancel.
        # On unexpected exceptions we keep it so the job auto-resumes on next boot.
        # (Does NOT run if the process is killed — the file survives in all kill cases.)
        if _clear_resume:
            _ar.clear_resume()
        bot_data["active_copy_task"]  = None
        bot_data["active_status_msg"] = None
        bot_data.pop("active_flood_wait",    None)
        bot_data.pop("active_copy_stats",    None)  # prevent stale stats on next job start
        bot_data.pop("active_copy_delay",    None)  # clear live speed override


async def _run_sync(client, src, dst, opts, bot, chat_id, bot_data):
    handler     = None
    status_msg  = None
    _last_edit  = 0.0          # throttle: don't edit more than once per 4 s
    _MIN_EDIT_GAP = 4.0

    try:
        # Send the initial "waiting" status message — we'll edit it on every forward
        filt_note = f"\n🔎 Filter: `{opts.get('filter_label', 'ALL').upper()}`" if opts["allowed_exts"] else ""
        skip_note = "\n🚫 Text-only messages skipped" if opts["skip_text"] else ""

        status_msg = await bot.send_message(
            chat_id,
            f"🔄 *Auto-Sync Active*{filt_note}{skip_note}\n\n"
            f"📡 `{src}` → `{dst}`\n\n"
            f"⏳ Waiting for new messages…\n"
            f"_Send /stopsync to stop._",
            parse_mode="Markdown",
        )
        status_msg_id = status_msg.message_id

        def _build_status(source_name, dest_name, stats, last_label=""):
            c = stats.get("copied",  0)
            f = stats.get("failed",  0)
            s = stats.get("skipped", 0)
            last_line = f"\n📨 Last: `{last_label}`" if last_label else ""
            return (
                f"🔄 *Auto-Sync Active*{filt_note}{skip_note}\n\n"
                f"📡 `{source_name}` → `{dest_name}`\n\n"
                f"✅ Sent: `{c:,}`  ❌ Failed: `{f:,}`  ⏭ Skipped: `{s:,}`"
                f"{last_line}\n\n"
                f"_Send /stopsync to stop._"
            )

        async def on_forwarded(msg, result, stats):
            nonlocal _last_edit
            now = time.time()
            if now - _last_edit < _MIN_EDIT_GAP:
                return
            _last_edit = now
            # Build a short label for what was just forwarded
            fname = getattr(getattr(msg, "file", None), "name", None) or ""
            if fname:
                last_label = fname[:50]
            elif getattr(msg, "message", None):
                last_label = (msg.message[:40] + "…") if len(msg.message) > 40 else msg.message
            else:
                last_label = f"msg #{msg.id}"
            try:
                await bot.edit_message_text(
                    _build_status(source_name, dest_name, stats, last_label),
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.debug("sync status edit failed: %s", e)

        handler, stats, source_name, dest_name = await start_sync_handler(
            client, src, dst,
            allowed_exts=opts["allowed_exts"],
            caption_replacement=opts["caption_replacement"],
            caption_suffix=opts.get("caption_suffix", ""),
            skip_text=opts["skip_text"],
            on_forwarded=on_forwarded,
        )
        bot_data["active_sync_handler"] = handler
        bot_data["active_sync_stats"]   = stats

        # Update the status message now that we have real channel names
        try:
            await bot.edit_message_text(
                _build_status(source_name, dest_name, stats),
                chat_id=chat_id,
                message_id=status_msg_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

        await asyncio.Event().wait()

    except asyncio.CancelledError:
        if handler:
            try:
                client.remove_event_handler(handler)
            except Exception:
                pass
        bot_data.pop("active_sync_handler", None)
        bot_data.pop("active_sync_opts",    None)   # bug-fix: clear on cancel
        bot_data.pop("active_sync_src",     None)
        bot_data.pop("active_sync_dst",     None)
        stats = bot_data.pop("active_sync_stats", {})
        c = stats.get("copied",  0)
        f = stats.get("failed",  0)
        s = stats.get("skipped", 0)
        summary = (
            f"🛑 *Auto-Sync Stopped*\n\n"
            f"✅ Sent: `{c:,}`  ❌ Failed: `{f:,}`  ⏭ Skipped: `{s:,}`"
        )
        try:
            if status_msg:
                await bot.edit_message_text(
                    summary, chat_id=chat_id,
                    message_id=status_msg.message_id,
                    parse_mode="Markdown",
                )
            else:
                await bot.send_message(chat_id, summary, parse_mode="Markdown")
        except Exception:
            try:
                await bot.send_message(chat_id, summary, parse_mode="Markdown")
            except Exception:
                pass

    except Exception as e:
        logger.exception("Sync task error")
        if handler:
            try:
                client.remove_event_handler(handler)
            except Exception:
                pass
        bot_data.pop("active_sync_handler", None)
        bot_data.pop("active_sync_stats",   None)
        bot_data.pop("active_sync_opts",    None)   # bug-fix: clear on error
        bot_data.pop("active_sync_src",     None)
        bot_data.pop("active_sync_dst",     None)
        try:
            await bot.send_message(chat_id, f"❌ Sync error: {e}")
        except Exception:
            pass

    finally:
        bot_data["active_sync_task"] = None


# ═══════════════════════════════════════════════════════════════════════════════
#  Standalone commands
# ═══════════════════════════════════════════════════════════════════════════════

def _build_status_text(bot_data: dict) -> str:
    """Build a status text string from current bot_data — shared by command and button."""
    import userbot_bridge as _bridge

    lines = ["📊 *Bot Status*\n"]

    # Userbot
    if _bridge.is_ready(bot_data):
        lines.append("🤖 *Userbot:* ✅ Connected")
    elif _bridge.is_locked(bot_data):
        lines.append("🤖 *Userbot:* ⏳ Session busy (retrying…)")
    elif bot_data.get("userbot_client") is not None:
        reason = bot_data.get("userbot_reason", "")
        if reason == "reconnecting" or reason == "connecting":
            lines.append("🤖 *Userbot:* 🔄 Reconnecting… (will resume shortly)")
        elif reason == "auth_failed":
            lines.append(
                "🤖 *Userbot:* ⚠️ Session invalid/revoked\n"
                "   → Generate a new session with /gensession"
            )
        elif reason == "needs_login":
            lines.append("🤖 *Userbot:* 🔑 Not logged in — use /login to sign in")
        else:
            lines.append("🤖 *Userbot:* 🔄 Starting up…")
    else:
        lines.append("🤖 *Userbot:* ❌ Not initialised")

    # Session-lost warning (set by bridge when copy was cancelled due to auth failure)
    if bot_data.get("session_lost_during_copy", False):
        lines.append(
            "\n⚠️ *Copy job was stopped* because the Telegram session was revoked.\n"
            "Use /gensession to create a fresh session, then /copy to restart."
        )

    # Forward rules
    rules = bot_data.get("forward_rules", {})
    lines.append(f"📋 *Forward rules:* {len(rules)} active")

    # Copy job
    copy_task = bot_data.get("active_copy_task")
    sync_hdlr = bot_data.get("active_sync_handler")

    if copy_task and not copy_task.done():
        stats = bot_data.get("active_copy_stats", {})
        c = stats.get("copied",  0)
        s = stats.get("skipped", 0)
        f = stats.get("failed",  0)
        t = stats.get("total",   0)
        total_note = f" / `{t:,}`" if t else ""

        # Check if a flood wait is currently active
        flood = bot_data.get("active_flood_wait")
        flood_line = ""
        if flood:
            remaining = int(flood["until"] - time.time())
            if remaining > 0:
                fm, fs = divmod(remaining, 60)
                wait_str = f"{fm}m {fs}s" if fm else f"{fs}s"
                flood_line = f"\n  ⏳ *Flood wait:* `{wait_str}` remaining — copy paused"
            else:
                bot_data.pop("active_flood_wait", None)

        # Check if this is a dual-copy job (stats_b key present)
        stats_b = bot_data.get("active_copy_stats_b")
        if stats_b is not None:
            cb = stats_b.get("copied",  0)
            sb2 = stats_b.get("skipped", 0)
            fb2 = stats_b.get("failed",  0)
            flood_b = bot_data.get("active_flood_wait_b")
            flood_b_line = ""
            if flood_b:
                rem_b = int(flood_b["until"] - time.time())
                if rem_b > 0:
                    fb_m, fb_s = divmod(rem_b, 60)
                    wt_b = f"{fb_m}m {fb_s}s" if fb_m else f"{fb_s}s"
                    flood_b_line = f"\n      ⏳ Flood wait: `{wt_b}` remaining"
            lines.append(
                f"\n🔀 *Dual Copy running*\n"
                f"  👤 Account A: ✅`{c:,}`{total_note} ⏭`{s:,}` ❌`{f:,}`{flood_line}\n"
                f"  👤 Account B: ✅`{cb:,}` ⏭`{sb2:,}` ❌`{fb2:,}`{flood_b_line}"
            )
        else:
            job_label = "⏸ *Copy job paused (flood wait)*" if flood_line else "▶ *Copy job running*"
            lines.append(
                f"\n{job_label}\n"
                f"  ✅ Copied: `{c:,}`{total_note}  "
                f"⏭ Skipped: `{s:,}`  ❌ Failed: `{f:,}`"
                f"{flood_line}"
            )
    elif sync_hdlr:
        stats = bot_data.get("active_sync_stats", {})
        c = stats.get("copied",  0)
        s = stats.get("skipped", 0)
        f = stats.get("failed",  0)
        lines.append(
            f"\n🔄 *Auto-Sync running*\n"
            f"  ✅ Sent: `{c:,}`  ⏭ Skipped: `{s:,}`  ❌ Failed: `{f:,}`"
        )
    else:
        lines.append("\n💤 *No active job*")
        # Show a hint if there is a saved checkpoint the user can resume from.
        # This covers two cases:
        #   a) autoresume.json exists — bot was killed mid-job and auto-resume
        #      hasn't fired yet (e.g. userbot still reconnecting at startup).
        #   b) autoresume.json is gone (job was cancelled) but the user wants
        #      to know there's progress saved: we check the default channel pair.
        pending = _ar.load_resume()
        if pending:
            p_src = pending.get("src", "?")
            p_dst = pending.get("dst", "?")
            lines.append(
                f"\n💾 *Checkpoint pending:* `{p_src}` → `{p_dst}`\n"
                f"_Use /resume to restart from last saved position._"
            )
        elif config.SOURCE_CHANNEL and config.DEST_CHANNEL:
            from userbot.checkpoint import exists as _ckpt_exists
            if _ckpt_exists(config.SOURCE_CHANNEL, config.DEST_CHANNEL):
                lines.append(
                    "\n💾 *Checkpoint available* for configured channel pair.\n"
                    "_Use /resume to continue from last saved position._"
                )

    return "\n".join(lines)


def _status_keyboard(from_menu: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="status_menu")]]
    if from_menu:
        rows.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 📊 Status button from the main menu."""
    query = update.callback_query
    await query.answer()
    text = _build_status_text(context.bot_data)
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=_status_keyboard(from_menu=True),
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = _build_status_text(context.bot_data)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_status_keyboard(from_menu=False),
    )


async def stopjob_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.bot_data.get("active_copy_task")
    if task and not task.done():
        # Delete the resume file NOW — synchronously, before task.cancel() and
        # before any possible Railway SIGKILL — so a subsequent restart cannot
        # silently auto-resume a job the user explicitly stopped.
        _ar.clear_resume()
        context.bot_data[_AR_CANCEL_KEY] = True   # abort any pending countdown
        task.cancel()
        is_dual = context.bot_data.get("active_copy_stats_b") is not None
        label = "dual copy job" if is_dual else "job"
        await update.message.reply_text(f"⛔ Stopping {label}…")
    else:
        await update.message.reply_text(
            "No copy job is currently running.\n"
            "Use /status to check bot state."
        )


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /resume — manually restart an interrupted copy job from its last checkpoint.

    Useful when:
      • auto-resume didn't fire (e.g. userbot took too long to connect at startup)
      • the user wants to restart after a manual /stopjob

    Priority:
      1. autoresume.json exists  → exact src/dst/opts from the interrupted run
      2. No autoresume.json but a checkpoint file exists for the configured channel
         pair → use config.py defaults (Safe speed, default filter, etc.)
      3. Nothing found           → tell the user to use /copy instead
    """
    bot_data = context.bot_data

    # Guard: don't start if a job is already running
    task = bot_data.get("active_copy_task")
    if task and not task.done():
        await update.message.reply_text(
            "⚠️ A copy job is already running.\n"
            "Use /status to see progress or /stopjob to cancel it."
        )
        return

    # Userbot must be connected
    if not bridge.is_ready(bot_data):
        locked = bridge.is_locked(bot_data)
        await update.message.reply_text(_not_ready(locked, bridge.is_starting_up(bot_data)), parse_mode="Markdown")
        return

    # ── Find what to resume ──────────────────────────────────────────────────

    src = dst = opts = None
    resume_file = _ar.load_resume()

    if resume_file:
        src  = resume_file["src"]
        dst  = resume_file["dst"]
        opts = resume_file["opts"]
    elif config.SOURCE_CHANNEL and config.DEST_CHANNEL:
        from userbot.checkpoint import exists as _ckpt_exists
        if _ckpt_exists(config.SOURCE_CHANNEL, config.DEST_CHANNEL):
            src = config.SOURCE_CHANNEL
            dst = config.DEST_CHANNEL
            opts = _default_opts("copy")
            # Override with live config values (may have changed since last run)
            opts.update({
                "allowed_exts":        set(config.ALLOWED_EXTS),
                "caption_replacement": config.CAPTION_REPLACE,
                "caption_suffix":      context.user_data.get("caption_suffix", config.CAPTION_SUFFIX),
                "notify_every":        config.NOTIFY_EVERY,
                "skip_text":           bool(config.SKIP_TEXT),
            })

    if src is None:
        await update.message.reply_text(
            "📭 *No checkpoint found.*\n\n"
            "There is no saved progress to resume from.\n"
            "Use /copy to start a new copy job.",
            parse_mode="Markdown",
        )
        return

    # Backfill any keys added to _default_opts after this save file was created.
    # Prevents KeyError when new opts keys are accessed on old save files.
    opts = {**_default_opts(opts.get("mode", "copy")), **opts}

    # ── Load checkpoint stats for the confirmation message ───────────────────
    from userbot.checkpoint import load as _ckpt_load
    copied = last_id = 0
    updated = "unknown"
    try:
        if isinstance(src, int) and isinstance(dst, int):
            ckpt = _ckpt_load(src, dst)
            copied   = ckpt.get("copied",      0)
            last_id  = ckpt.get("last_msg_id", 0)
            updated  = ckpt.get("updated_at",  "unknown") or "unknown"
    except Exception:
        pass  # non-critical — display falls back to zeros

    # ── Human-readable speed label ────────────────────────────────────────────
    rate_delay = opts.get("rate_delay", _SPEED_CYCLE[0][1])
    speed_label = f"`{rate_delay}s/file`"
    for lbl, delay in _SPEED_CYCLE:
        if abs(delay - rate_delay) < 0.001:
            speed_label = f"`{lbl}`"
            break

    # ── Store pending state for the confirm callback ──────────────────────────
    context.user_data["pending_resume"] = {
        "src":     src,
        "dst":     dst,
        "opts":    opts,
        "chat_id": update.effective_chat.id,
    }

    await update.message.reply_text(
        f"♻️ *Resume Copy Job*\n\n"
        f"📡 `{src}` → `{dst}`\n"
        f"📌 Progress: `{copied:,}` files copied\n"
        f"🔖 Last msg ID: `{last_id:,}`\n"
        f"🕐 Checkpoint: `{updated}`\n"
        f"⏩ Speed: {speed_label}\n\n"
        f"_Tap ▶ Resume to continue from the checkpoint._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶ Resume",  callback_data=_RESUME_CONFIRM),
            InlineKeyboardButton("❌ Cancel", callback_data=_RESUME_CANCEL),
        ]]),
    )


async def resume_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the ▶ Resume / ❌ Cancel buttons from /resume."""
    query = update.callback_query
    await query.answer()

    if query.data == _RESUME_CANCEL:
        await query.edit_message_text(
            "❌ *Cancelled.* No job was started.\n\n"
            "Use /resume again whenever you're ready.",
            parse_mode="Markdown",
        )
        context.user_data.pop("pending_resume", None)
        return

    # ── Resume confirmed ─────────────────────────────────────────────────────
    pending = context.user_data.pop("pending_resume", None)
    if not pending:
        await query.edit_message_text(
            "⚠️ *Session expired.* Please run /resume again.",
            parse_mode="Markdown",
        )
        return

    bot_data = context.bot_data

    # Guard: a job might have started between /resume and the button tap
    task = bot_data.get("active_copy_task")
    if task and not task.done():
        await query.edit_message_text(
            "⚠️ Another copy job started while you were deciding.\n"
            "Use /status to check it.",
            parse_mode="Markdown",
        )
        return

    # Userbot still needs to be connected
    if not bridge.is_ready(bot_data):
        await query.edit_message_text(
            "❌ *Userbot not connected.*\n\n"
            "Wait a moment for it to reconnect, then run /resume again.",
            parse_mode="Markdown",
        )
        return

    src     = pending["src"]
    dst     = pending["dst"]
    opts    = pending["opts"]
    chat_id = query.message.chat_id
    bot     = context.application.bot
    client  = bridge.get_client(bot_data)

    if client is None:
        await query.edit_message_text(
            "❌ *Userbot client unavailable.*\n\n"
            "Run /resume again once it reconnects.",
            parse_mode="Markdown",
        )
        return

    # Merge user caption_suffix into opts before saving (in case job was
    # started before /setcaption was configured, or opts came from an old save)
    opts.setdefault("caption_suffix", context.user_data.get("caption_suffix", config.CAPTION_SUFFIX))
    # Write autoresume file so a crash/kill during this resumed job restarts it
    _ar.save_resume(chat_id, src, dst, opts)

    await query.edit_message_text(
        f"▶ *Resuming copy job…*\n\n"
        f"📡 `{src}` → `{dst}`\n\n"
        f"_Continuing from last checkpoint._\n"
        f"_Use /stopjob to cancel._",
        parse_mode="Markdown",
    )

    notifier = BotProgressNotifier(
        bot, chat_id, query.message.message_id,
        every=opts.get("notify_every", 100),
        bot_data=bot_data,
    )

    task = asyncio.create_task(
        _run_copy(client, src, dst, opts, notifier, bot, chat_id, bot_data)
    )
    bot_data["active_copy_task"]  = task
    bot_data["active_status_msg"] = (chat_id, query.message.message_id)
    logger.info("Manual /resume: copy task launched for src=%s dst=%s", src, dst)


async def stopsync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.bot_data.get("active_sync_task")
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("⛔ Stopping auto-sync…")
        return

    handler = context.bot_data.get("active_sync_handler")
    if handler:
        client = bridge.get_client(context.bot_data)
        if client:
            try:
                client.remove_event_handler(handler)
            except Exception:
                pass
        context.bot_data.pop("active_sync_handler", None)
        context.bot_data.pop("active_sync_opts",    None)   # bug-fix: clear on stop
        stats = context.bot_data.pop("active_sync_stats", {})
        c = stats.get("copied",  0)
        s = stats.get("skipped", 0)
        f = stats.get("failed",  0)
        await update.message.reply_text(
            f"🛑 *Auto-Sync Stopped*\n\n"
            f"✅ Sent: `{c:,}`  ❌ Failed: `{f:,}`  ⏭ Skipped: `{s:,}`",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("Auto-sync is not running.")


async def _send_chats_list(chats: list, first_edit_coro, send_coro) -> None:
    """
    Shared helper — renders the chats list and sends it in ≤4000-char chunks.
    first_edit_coro(text) edits the "loading" placeholder message.
    send_coro(text)       sends any overflow chunks as new messages.
    """
    ICONS = {"channel": "📡", "group": "👥", "user": "👤", "bot": "🤖"}
    order = ["channel", "group", "user", "bot"]
    groups: dict[str, list] = {t: [] for t in order}
    for c in chats:
        groups.setdefault(c["type"], []).append(c)

    lines = [f"📋 *Your Chats* ({len(chats)} total)\n"]
    for t in order:
        if not groups[t]:
            continue
        icon = ICONS.get(t, "💬")
        lines.append(f"\n{icon} *{t.title()}s*")
        for c in groups[t]:
            name = c["name"][:38]
            lines.append(f"`{str(c['id']):<22}` {name}")

    chunk, chunk_len = [], 0
    first = True
    for line in lines:
        if chunk_len + len(line) + 1 > 4000 and chunk:
            text = "\n".join(chunk)
            if first:
                await first_edit_coro(text)
                first = False
            else:
                await send_coro(text)
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line) + 1

    if chunk:
        text = "\n".join(chunk)
        if first:
            await first_edit_coro(text)
        else:
            await send_coro(text)


async def listchats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 📡 List My Chats button from the main menu."""
    query = update.callback_query
    await query.answer()

    if not bridge.is_ready(context.bot_data):
        locked = bridge.is_locked(context.bot_data)
        await query.edit_message_text(
            _not_ready(locked, bridge.is_starting_up(context.bot_data)),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
            ),
        )
        return

    await query.edit_message_text("⏳ Loading your chats…")

    client = bridge.get_client(context.bot_data)
    chat_id = query.message.chat_id
    msg_id  = query.message.message_id
    bot     = context.application.bot

    try:
        chats = await list_chats(client)
    except Exception as e:
        await bot.edit_message_text(
            f"❌ Failed to load chats: {e}",
            chat_id=chat_id, message_id=msg_id,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
            ),
        )
        return

    if not chats:
        await bot.edit_message_text(
            "No chats found.",
            chat_id=chat_id, message_id=msg_id,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
            ),
        )
        return

    back_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]]
    )

    async def first_edit(text):
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=msg_id,
            parse_mode="Markdown", reply_markup=back_kb,
        )

    async def overflow_send(text):
        await bot.send_message(chat_id, text, parse_mode="Markdown")

    await _send_chats_list(chats, first_edit, overflow_send)


async def listchats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the user a list of all their Telegram chats with IDs."""
    if not bridge.is_ready(context.bot_data):
        locked = bridge.is_locked(context.bot_data)
        await update.message.reply_text(_not_ready(locked, bridge.is_starting_up(context.bot_data)), parse_mode="Markdown")
        return

    loading = await update.message.reply_text("⏳ Loading your chats…")
    client  = bridge.get_client(context.bot_data)

    try:
        chats = await list_chats(client)
    except Exception as e:
        await loading.edit_text(f"❌ Failed to load chats: {e}")
        return

    if not chats:
        await loading.edit_text("No chats found.")
        return

    async def first_edit(text):
        await loading.edit_text(text, parse_mode="Markdown")

    async def overflow_send(text):
        await update.message.reply_text(text, parse_mode="Markdown")

    await _send_chats_list(chats, first_edit, overflow_send)


async def got_replace_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called after user types their replacement username (or 'off')."""
    text = update.message.text.strip()
    opts = context.user_data.get("copy_opts", {})

    if text.lower() in ("off", "none", "-", ""):
        opts["caption_replacement"] = ""
        confirm = "✅ Username replacement *disabled* — original @usernames will be kept."
    else:
        # Ensure it starts with @
        username = text if text.startswith("@") else f"@{text}"
        opts["caption_replacement"] = username
        confirm = f"✅ All `@username` mentions will be replaced with `{username}`."

    src_raw = context.user_data.get("copy_src_raw", "?")
    dst_raw = context.user_data.get("copy_dst_raw", "?")

    _dual_avail2 = context.bot_data and bridge.is_ready_2(context.bot_data)
    await update.message.reply_text(
        confirm + "\n\n" + _opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_opts_keyboard(opts, dual_available=_dual_avail2),
    )
    return COPY_OPTIONS


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled. Use /copy, /dryrun, or /sync to start."
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  /history — show per-channel-pair copy stats from checkpoint files
# ═══════════════════════════════════════════════════════════════════════════════

def _load_all_checkpoints() -> list[dict]:
    """
    Read every checkpoint JSON file and return a list of lightweight dicts.

    Memory fix: we only need the COUNT of copied_ids, not the ids themselves
    (which can be tens of thousands of integers). Extract the count and discard
    the list so /history doesn't load megabytes of data just to display stats.

    Records are sorted by updated_at descending (most recent first).
    Filenames are <src_id>_<dst_id>.json — alphabetical sort of numeric names
    gives wrong order for different-length IDs, so we sort by timestamp instead.
    """
    records = []
    if not os.path.isdir(_CHECKPOINTS_DIR):
        return records
    for fname in os.listdir(_CHECKPOINTS_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(_CHECKPOINTS_DIR, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
            # Store just the count — never load the full id list into memory
            data["_done_ids_count"] = len(data.get("copied_ids", []))
            data.pop("copied_ids", None)
            data["_file"] = fname
            records.append(data)
        except Exception:
            pass
    # Most-recently-updated first; fall back to started_at, then filename
    records.sort(
        key=lambda r: r.get("updated_at") or r.get("started_at") or r["_file"],
        reverse=True,
    )
    return records


async def _resolve_name(client, chat_id) -> str:
    """
    Try to resolve a channel name from Telethon; fall back to the raw ID.
    Guards against non-int IDs (e.g. missing-key default "?") to avoid
    firing a doomed API call.
    """
    if not isinstance(chat_id, int):
        return str(chat_id)
    try:
        entity = await client.get_entity(chat_id)
        return (
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or str(chat_id)
        )
    except Exception:
        return str(chat_id)


def _md_safe(text: str) -> str:
    """
    Escape Telegram Markdown V1 special characters in free-form text
    (channel names, timestamps) so they don't break parse_mode='Markdown'.
    """
    for ch in ("*", "_", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _code(text: str) -> str:
    """Wrap arbitrary text in a backtick code span, escaping inner backticks.
    Code spans are safe from Telegram Markdown V1 special-character parsing,
    so channel names / patterns with *, _, [ etc. render correctly."""
    return "`" + str(text).replace("`", "'") + "`"


async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /config — show every active config setting at a glance.
    Displays static config.py defaults plus any active sync-job overrides.
    """
    client = bridge.get_client(context.bot_data)

    # ── Resolve channel names (2 calls max, errors fall back to raw ID) ──────
    async def _name(ch_id) -> str:
        if not ch_id or not isinstance(ch_id, int):
            return "_not set_"
        return _code(await _resolve_name(client, ch_id) if client else str(ch_id))

    src_disp = await _name(config.SOURCE_CHANNEL)
    dst_disp = await _name(config.DEST_CHANNEL)

    # ── Caption replacement ───────────────────────────────────────────────────
    repl      = config.CAPTION_REPLACE
    repl_disp = _code(repl) if repl else "_keep original_"

    # ── Caption suffix ────────────────────────────────────────────────────────
    suffix_env     = config.CAPTION_SUFFIX
    suffix_session = context.user_data.get("caption_suffix", suffix_env)
    if suffix_session:
        suffix_disp = _code(suffix_session)
        if suffix_session != suffix_env:
            suffix_disp += " _(session override — /setcaption off to reset)_"
    else:
        suffix_disp = "_none_"

    # ── File filter ───────────────────────────────────────────────────────────
    exts      = config.ALLOWED_EXTS or set()
    exts_disp = (
        ", ".join(_code(e.upper()) for e in sorted(exts))
        if exts else "_ALL files_"
    )

    # ── Notifications (NOTIFY_EVERY = 0 means off) ───────────────────────────
    notify      = config.NOTIFY_EVERY
    notify_disp = f"every {_code(notify)} files" if notify else "_off_"

    # ── Strip patterns ────────────────────────────────────────────────────────
    patterns = config.STRIP_PATTERNS or []
    if patterns:
        shown   = patterns[:8]
        pat_txt = "\n".join(f"   • {_code(p)}" for p in shown)
        if len(patterns) > 8:
            pat_txt += f"\n   _…and {len(patterns) - 8} more_"
    else:
        pat_txt = "   _none configured_"

    # ── Skip text setting ─────────────────────────────────────────────────────
    skip_disp = "✅ Skip all text-only messages" if config.SKIP_TEXT else "❌ Copy text messages too"

    lines = [
        "⚙️ *Bot Configuration*\n",
        f"📡 *Source channel:*\n   ID: {_code(config.SOURCE_CHANNEL or 'not set')}  {src_disp}",
        f"📥 *Dest channel:*\n   ID: {_code(config.DEST_CHANNEL   or 'not set')}  {dst_disp}",
        f"✏️ *Caption replacement:* {repl_disp}",
        f"➕ *Caption suffix:* {suffix_disp}",
        f"📁 *File filter:* {exts_disp}",
        f"🚫 *Text messages:* {skip_disp}",
        f"🔔 *Notifications:* {notify_disp}",
        f"✂️ *Strip patterns* ({len(patterns)}):\n{pat_txt}",
    ]

    # ── Active sync-job overrides (shown when /sync is running) ──────────────
    sync_opts = context.bot_data.get("active_sync_opts")
    if sync_opts:
        o_repl  = sync_opts.get("caption_replacement", config.CAPTION_REPLACE)
        o_skip  = sync_opts.get("skip_text", config.SKIP_TEXT)
        o_label = sync_opts.get("filter_label", "ALL")

        o_repl_disp = _code(o_repl) if o_repl else "_keep original_"
        o_exts_disp = o_label if o_label != "ALL" else "_ALL files_"
        o_skip_disp = "✅ skip" if o_skip else "❌ copy"

        lines.append(
            f"\n🔄 *Active /sync overrides:*\n"
            f"   ✏️ Replacement: {o_repl_disp}\n"
            f"   📁 Filter: {_code(o_exts_disp)}\n"
            f"   🚫 Text: {o_skip_disp}"
        )

    # ── Active copy-job status ────────────────────────────────────────────────
    copy_task = context.bot_data.get("active_copy_task")
    if copy_task and not copy_task.done():
        lines.append("\n▶️ *A /copy job is currently running.* Use /status for progress.")

    lines.append("\n_Edit Railway Variables to change defaults. Use /setcaption to override suffix for the current session._")

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
#  /clearhistory — delete checkpoint files so a channel pair can be re-copied
# ═══════════════════════════════════════════════════════════════════════════════

_CH_SELECT  = "clrhist:"       # show confirm for one file
_CH_YES     = "clrhist_yes:"   # confirmed delete one
_CH_NO      = "clrhist_no"     # cancel
_CH_ALL     = "clrhist_all"    # show confirm for all
_CH_ALL_YES = "clrhist_allyes" # confirmed delete all


def _btn_label(name: str, max_len: int = 22) -> str:
    """Truncate a channel name so it fits cleanly inside an InlineKeyboard button."""
    return name if len(name) <= max_len else name[: max_len - 1] + "…"


async def clearhistory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /clearhistory — list checkpoints with delete buttons.
    Deleting a checkpoint allows that channel pair to be re-copied from scratch
    while leaving all other pairs untouched.
    """
    records = _load_all_checkpoints()
    if not records:
        await update.message.reply_text(
            "📭 *No copy history yet* — nothing to clear.",
            parse_mode="Markdown",
        )
        return

    client = bridge.get_client(context.bot_data)

    # Resolve names for button labels (best-effort; falls back to raw ID)
    rows = []
    for i, rec in enumerate(records, 1):
        src_id = rec.get("source_id")
        dst_id = rec.get("dest_id")
        fname  = rec["_file"]
        done   = rec.get("_done_ids_count", 0)

        if client:
            src_name = await _resolve_name(client, src_id)
            dst_name = await _resolve_name(client, dst_id)
        else:
            src_name = str(src_id)
            dst_name = str(dst_id)

        label = (
            f"{i}. {_btn_label(src_name)} → {_btn_label(dst_name)} "
            f"({done:,} IDs)"
        )
        # callback_data encodes the filename — immune to index changes
        cb = _CH_SELECT + fname
        rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=cb)])

    rows.append([
        InlineKeyboardButton(
            f"🗑🗑 Clear ALL {len(records)} checkpoint(s)",
            callback_data=_CH_ALL,
        )
    ])

    await update.message.reply_text(
        "🗂 *Clear Copy History*\n\n"
        "Tap a row to delete that checkpoint.\n"
        "Deleting lets you re-copy that channel pair from scratch.\n"
        "_All other pairs remain untouched._",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )


async def clearhistory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all /clearhistory inline button presses."""
    query = update.callback_query
    await query.answer()          # always answer first — stops Telegram's spinner
    data  = query.data or ""

    # ── Helper: safe edit (original message may have been deleted) ────────────
    async def _edit(text: str, markup=None):
        try:
            await query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=markup
            )
        except Exception:
            await query.message.reply_text(text, parse_mode="Markdown")

    # ── Cancel ────────────────────────────────────────────────────────────────
    if data == _CH_NO:
        await _edit("❌ *Cancelled.* No files were deleted.")
        return

    # ── Show confirmation for a single file ───────────────────────────────────
    if data.startswith(_CH_SELECT):
        fname = data[len(_CH_SELECT):]
        fpath = os.path.join(_CHECKPOINTS_DIR, fname)
        if not os.path.exists(fpath):
            await _edit("⚠️ *Already deleted* — that checkpoint no longer exists.")
            return

        # Parse src/dst from filename for the confirm message
        stem = fname.replace(".json", "")
        parts = stem.split("_", 1)
        pair  = f"`{parts[0]}` → `{parts[1]}`" if len(parts) == 2 else f"`{fname}`"

        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete it", callback_data=_CH_YES + fname),
            InlineKeyboardButton("❌ Cancel",         callback_data=_CH_NO),
        ]])
        await _edit(
            f"⚠️ *Are you sure?*\n\n"
            f"Delete checkpoint for {pair}?\n\n"
            f"The bot will re-copy this channel pair from the beginning "
            f"on the next run (all other pairs are unaffected).",
            markup=markup,
        )
        return

    # ── Confirmed: delete single file ─────────────────────────────────────────
    if data.startswith(_CH_YES):
        fname = data[len(_CH_YES):]
        fpath = os.path.join(_CHECKPOINTS_DIR, fname)
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
                await _edit(f"✅ *Deleted* — `{fname}` removed.\n\nRun /copy to start fresh.")
            except OSError as exc:
                await _edit(f"❌ *Error deleting file:*\n`{exc}`")
        else:
            await _edit("⚠️ *Already gone* — that checkpoint was already deleted.")
        return

    # ── Show confirmation for clearing ALL ────────────────────────────────────
    if data == _CH_ALL:
        count = len(_load_all_checkpoints())
        if count == 0:
            await _edit("📭 *No checkpoints left* — nothing to clear.")
            return
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Yes, delete all {count}", callback_data=_CH_ALL_YES),
            InlineKeyboardButton("❌ Cancel",                   callback_data=_CH_NO),
        ]])
        await _edit(
            f"⚠️ *Delete ALL {count} checkpoint(s)?*\n\n"
            f"Every channel pair will be re-copied from scratch on the next run.",
            markup=markup,
        )
        return

    # ── Confirmed: delete all files ───────────────────────────────────────────
    if data == _CH_ALL_YES:
        records = _load_all_checkpoints()
        deleted, failed = 0, 0
        for rec in records:
            fpath = os.path.join(_CHECKPOINTS_DIR, rec["_file"])
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    deleted += 1
                except OSError:
                    failed += 1
        msg = f"✅ *Cleared {deleted} checkpoint(s).*"
        if failed:
            msg += f"\n⚠️ {failed} file(s) could not be deleted."
        msg += "\n\nRun /copy to start all channel pairs fresh."
        await _edit(msg)
        return


async def copystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /history — list all channel-pair checkpoints with duplicate-protection stats.
    Shows how many files have already been copied and are safe from re-copying.
    """
    records = _load_all_checkpoints()

    if not records:
        await update.message.reply_text(
            "📭 *No copy history yet.*\n\n"
            "Run /copy to start copying — every file sent is recorded here "
            "so future runs never send duplicates.",
            parse_mode="Markdown",
        )
        return

    client = bridge.get_client(context.bot_data)

    # Name-resolution cache: each unique channel ID is fetched at most once
    # (many records may share the same destination, so caching matters).
    _name_cache: dict = {}

    async def resolve(chat_id) -> str:
        if chat_id not in _name_cache:
            _name_cache[chat_id] = (
                await _resolve_name(client, chat_id) if client else str(chat_id)
            )
        return _name_cache[chat_id]

    header  = "📋 *Copy History — Duplicate Protection Stats*"
    entries = []
    for i, rec in enumerate(records, 1):
        src_id      = rec.get("source_id")
        dst_id      = rec.get("dest_id")
        copied      = rec.get("copied",    0)
        skipped     = rec.get("skipped",   0)
        failed      = rec.get("failed",    0)
        done_ids    = rec.get("_done_ids_count", 0)
        last_msg_id = rec.get("last_msg_id", 0)
        updated     = rec.get("updated_at") or rec.get("started_at") or "—"

        src_name = _md_safe(await resolve(src_id))
        dst_name = _md_safe(await resolve(dst_id))

        # Show whether the job appears complete or was interrupted mid-run
        status = ""
        if last_msg_id and last_msg_id > 0:
            status = f"   📍 Last msg ID: `{last_msg_id:,}`\n"

        entries.append(
            f"*{i}.* `{src_name}` → `{dst_name}`\n"
            f"   🛡 Protected IDs: `{done_ids:,}` "
            f" ✅ `{copied:,}` ⏭ `{skipped:,}` ❌ `{failed:,}`\n"
            f"{status}"
            f"   🕒 {updated}"
        )

    total_protected = sum(r.get("_done_ids_count", 0) for r in records)
    footer = f"\n🔒 *Total files protected from duplicates: `{total_protected:,}`*"

    # ── Message-length guard ──────────────────────────────────────────────────
    # Build pages of ≤ 4000 chars so we never hit Telegram's 4096-char limit.
    pages: list[str] = []
    current = header
    for entry in entries:
        block = "\n\n" + entry
        if len(current) + len(block) > 3900:
            pages.append(current)
            current = f"📋 *Copy History (continued)*{block}"
        else:
            current += block
    current += footer
    pages.append(current)

    for page in pages:
        await update.message.reply_text(page, parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
#  Auto-resume — restart an interrupted copy job on bot startup
# ═══════════════════════════════════════════════════════════════════════════════

_AR_CANCEL_KEY = "ar_cancel_pending"
_AR_COUNTDOWN  = 20   # seconds the user has to cancel before the job starts
_AR_MISMATCH_KEY = "ar_mismatch_resume"  # bot_data key holding a pending mismatch resume

_AR_CANCEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ Cancel Auto-Resume", callback_data="ar_cancel")],
])


async def ar_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline button — user cancels the pending auto-resume countdown."""
    query = update.callback_query
    await query.answer("Auto-resume cancelled.")
    context.bot_data[_AR_CANCEL_KEY] = True
    _ar.clear_resume()
    try:
        await query.edit_message_text(
            "❌ *Auto-resume cancelled.*\n\n"
            "The checkpoint has been deleted.\n"
            "Use /copy to start a new job, or /resume if you want to retry.",
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def ar_resume_saved_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mismatch alert — user chose to resume using the *saved* channels anyway."""
    query  = update.callback_query
    await query.answer("▶️ Resuming with saved channels…")
    resume = context.bot_data.pop(_AR_MISMATCH_KEY, None)
    if not resume:
        try:
            await query.edit_message_text(
                "⚠️ *Resume data expired.*\n\n"
                "The checkpoint is no longer available. Use /copy to start a new job.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return
    src = resume.get("src")
    dst = resume.get("dst")
    try:
        await query.edit_message_text(
            f"▶️ *Resuming with saved channels*\n\n"
            f"📡 `{src}` → `{dst}`\n\n"
            f"Starting in {_AR_COUNTDOWN} seconds — use /stopjob to cancel.",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    asyncio.create_task(
        _ar_launch_from_mismatch(update.effective_chat.id, resume, context.application)
    )


async def ar_cancel_saved_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mismatch alert — user chose to cancel and discard the checkpoint."""
    query = update.callback_query
    await query.answer("Checkpoint cleared.")
    context.bot_data.pop(_AR_MISMATCH_KEY, None)
    try:
        await query.edit_message_text(
            "❌ *Auto-resume cancelled.*\n\n"
            "The checkpoint has been deleted. Use /copy to start a new job.",
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def _ar_launch_from_mismatch(chat_id: int, resume: dict, application) -> None:
    """Launch a resumed job straight from the mismatch-override flow (skips guard)."""
    loop     = asyncio.get_running_loop()
    deadline = loop.time() + 90
    while not bridge.is_ready(application.bot_data):
        if loop.time() > deadline:
            logger.warning("Auto-resume (mismatch override): timed out waiting for userbot.")
            return
        await asyncio.sleep(2)
    application.bot_data[_AR_CANCEL_KEY] = False
    await _auto_resume_start(application, resume, None)


async def _auto_resume_start(application, resume: dict, countdown_msg_id: int | None) -> None:
    """
    Launch a copy job from persisted auto-resume state (no user interaction).
    countdown_msg_id is the message we sent during the countdown — we reuse
    it as the progress message so the user sees a smooth transition.
    """
    bot      = application.bot
    bot_data = application.bot_data
    chat_id  = resume["chat_id"]
    src      = resume["src"]
    dst      = resume["dst"]
    opts     = resume["opts"]

    # NOTE: we do NOT override src/dst from config here.
    # channel_settings.py guarantees that config.SOURCE_CHANNEL and
    # config.DEST_CHANNEL always equal the last /setsource and /setdest values,
    # and autoresume.clear_resume() is called whenever channels change — so the
    # stored src/dst in autoresume.json is always the correct target.
    # Overriding here would break any resume whose channels differ from env vars.

    client = bridge.get_client(bot_data)
    if client is None:
        logger.error("Auto-resume: client is None after bridge reported ready — aborting")
        return

    # Guard — do not start if another job somehow sneaked in
    existing = bot_data.get("active_copy_task")
    if existing and not existing.done():
        logger.info("Auto-resume: another copy job is already running — skipping resume")
        _ar.clear_resume()
        return

    # Edit the countdown message → "Resuming…" so progress updates follow naturally
    msg_id = countdown_msg_id
    try:
        if msg_id:
            await bot.edit_message_text(
                "♻️ *Auto-Resume — Starting…*\n\n"
                f"📡 `{src}` → `{dst}`\n\n"
                "_Use /stopjob to cancel at any time._",
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.debug("Auto-resume: could not edit countdown message: %s", e)

    notifier = BotProgressNotifier(
        bot, chat_id, msg_id,
        every=opts.get("notify_every", 100),
        bot_data=bot_data,
    )

    task = asyncio.create_task(
        _run_copy(client, src, dst, opts, notifier, bot, chat_id, bot_data)
    )
    bot_data["active_copy_task"]  = task
    bot_data["active_status_msg"] = (chat_id, msg_id) if msg_id else None
    logger.info("Auto-resume: copy task created for src=%s dst=%s", src, dst)


async def schedule_auto_resume(application) -> None:
    """
    Called once at startup (via asyncio.create_task in post_init).
    Waits for the userbot to be ready, shows a countdown with a Cancel button,
    then starts the job unless the user cancels within _AR_COUNTDOWN seconds.
    """
    resume = _ar.claim_resume()
    if not resume:
        return  # nothing to resume — fast path

    chat_id = resume.get("chat_id")
    src     = resume.get("src")
    dst     = resume.get("dst")

    # ── Channel mismatch guard ─────────────────────────────────────────────────
    # Only compare against configured (non-zero) values.  DEST_CHANNEL defaults
    # to 0 when the user has never run /setdest — comparing that against a real
    # saved dst would always trigger a false mismatch.
    _cfg_src = config.SOURCE_CHANNEL
    _cfg_dst = config.DEST_CHANNEL
    src_mismatch = bool(_cfg_src) and src != _cfg_src
    dst_mismatch = bool(_cfg_dst) and dst != _cfg_dst
    if src_mismatch or dst_mismatch:
        logger.warning(
            "Auto-resume: channel mismatch — saved (src=%s dst=%s) vs config (src=%s dst=%s). "
            "Asking user whether to resume with saved channels.",
            src, dst, _cfg_src, _cfg_dst,
        )
        # Capture in bot_data so the user can still launch with saved channels.
        application.bot_data[_AR_MISMATCH_KEY] = resume
        _ar.clear_resume()   # remove from disk; data is now in bot_data
        _mm_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Resume with saved channels", callback_data="ar_resume_saved"),
            InlineKeyboardButton("🚫 Cancel",                      callback_data="ar_cancel_saved"),
        ]])
        try:
            await application.bot.send_message(
                chat_id,
                f"⚠️ *Auto-Resume: Channel Mismatch*\n\n"
                f"The saved job used *different channels* than your current settings:\n\n"
                f"💾 *Saved:*   `{src}` → `{dst}`\n"
                f"⚙️ *Current:* `{_cfg_src or 'not set'}` → `{_cfg_dst or 'not set'}`\n\n"
                f"What would you like to do?",
                parse_mode="Markdown",
                reply_markup=_mm_kb,
            )
        except Exception as _me:
            logger.warning("Auto-resume: could not send mismatch notice: %s", _me)
        return

    logger.info(
        "Auto-resume: found saved job (src=%s dst=%s chat=%s) — waiting for userbot…",
        src, dst, chat_id,
    )

    # Poll until userbot is connected (up to 90 s)
    loop     = asyncio.get_running_loop()
    deadline = loop.time() + 90
    while not bridge.is_ready(application.bot_data):
        if loop.time() > deadline:
            logger.warning("Auto-resume: timed out waiting for userbot after 90 s — aborting.")
            return
        await asyncio.sleep(2)

    # ── Send countdown notice with Cancel button ──────────────────────────────
    application.bot_data[_AR_CANCEL_KEY] = False
    bot = application.bot
    countdown_msg_id = None
    try:
        msg = await bot.send_message(
            chat_id,
            f"♻️ *Auto-Resume Pending*\n\n"
            f"The bot restarted while a copy job was in progress.\n"
            f"📡 `{src}` → `{dst}`\n\n"
            f"⏳ Starting automatically in *{_AR_COUNTDOWN} seconds*…\n\n"
            f"Tap the button below to cancel, or do nothing to let it resume.",
            parse_mode="Markdown",
            reply_markup=_AR_CANCEL_KB,
        )
        countdown_msg_id = msg.message_id
    except Exception as e:
        logger.warning("Auto-resume: could not send countdown message to %s: %s", chat_id, e)

    # ── Wait for countdown, checking cancel flag every second ─────────────────
    for remaining in range(_AR_COUNTDOWN - 1, 0, -1):
        await asyncio.sleep(1)
        if application.bot_data.get(_AR_CANCEL_KEY):
            logger.info("Auto-resume: cancelled by user during countdown.")
            application.bot_data.pop(_AR_CANCEL_KEY, None)
            return
        # Update the message every 5 s so the user sees it counting down
        if remaining % 5 == 0 and countdown_msg_id:
            try:
                await bot.edit_message_text(
                    f"♻️ *Auto-Resume Pending*\n\n"
                    f"📡 `{src}` → `{dst}`\n\n"
                    f"⏳ Starting in *{remaining} seconds*…\n\n"
                    f"Tap below to cancel.",
                    chat_id=chat_id,
                    message_id=countdown_msg_id,
                    parse_mode="Markdown",
                    reply_markup=_AR_CANCEL_KB,
                )
            except Exception:
                pass

    # Final cancel check before launching
    if application.bot_data.get(_AR_CANCEL_KEY):
        logger.info("Auto-resume: cancelled by user just before launch.")
        application.bot_data.pop(_AR_CANCEL_KEY, None)
        return

    application.bot_data.pop(_AR_CANCEL_KEY, None)
    logger.info("Auto-resume: countdown elapsed — launching resumed job")
    await _auto_resume_start(application, resume, countdown_msg_id)


# ═══════════════════════════════════════════════════════════════════════════════
#  Handler factories — called by bot.py
# ═══════════════════════════════════════════════════════════════════════════════

def build_copy_conv() -> ConversationHandler:
    """ConversationHandler for /copy, /dryrun, /sync wizards."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("copy",   copy_cmd),
            CommandHandler("dryrun", dryrun_cmd),
            CommandHandler("sync",   sync_cmd),
        ],
        states={
            COPY_AWAIT_SRC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_source),
            ],
            COPY_AWAIT_DST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_dest),
            ],
            COPY_OPTIONS: [
                CallbackQueryHandler(options_callback, pattern="^copt_"),
            ],
            COPY_AWAIT_REPLACE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_replace_username),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_chat=False,
        per_user=True,
        per_message=False,
        allow_reentry=True,
    )


_SYNCTEST_TIMEOUT = 15   # seconds to wait for probe to arrive in dest


async def synctest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /synctest — one-tap sync health check.

    Sends a unique text probe to the source channel via the userbot.
    The active sync handler catches it and forwards it to the destination.
    We poll the destination for up to TIMEOUT seconds, report latency on
    success or a detailed failure reason on timeout.
    Both probe messages are deleted afterwards.
    """
    bot_data = context.bot_data

    # ── 1. Userbot must be ready ─────────────────────────────────────────────
    if not bridge.is_ready(bot_data):
        await update.message.reply_text(
            _not_ready(bridge.is_locked(bot_data), bridge.is_starting_up(bot_data)), parse_mode="Markdown"
        )
        return

    # ── 2. Sync must be running ──────────────────────────────────────────────
    if bot_data.get("active_sync_handler") is None:
        await update.message.reply_text(
            "❌ *Auto-sync is not running.*\n\n"
            "Start it with /sync first, then run /synctest to confirm it's live.",
            parse_mode="Markdown",
        )
        return

    # ── 3. Pre-flight: check if active filter would swallow a text probe ─────
    opts       = bot_data.get("active_sync_opts", {})
    skip_text  = opts.get("skip_text", False)
    allowed_ex = opts.get("allowed_exts", set())

    if skip_text or allowed_ex:
        reason = (
            "text-only messages are being *skipped*"
            if skip_text
            else f"filter is set to `{opts.get('filter_label', 'FILES')}` only"
        )
        await update.message.reply_text(
            f"⚠️ *Cannot run synctest — probe would be filtered.*\n\n"
            f"Your active sync is configured so {reason}.\n"
            f"A plain-text probe won't be forwarded, so the test would always timeout.\n\n"
            f"👉 Stop sync with /stopsync, restart with no filter, then retry /synctest.",
            parse_mode="Markdown",
        )
        return

    client = bridge.get_client(bot_data)
    src    = bot_data.get("active_sync_src") or config.SOURCE_CHANNEL
    dst    = bot_data.get("active_sync_dst") or config.DEST_CHANNEL

    status = await update.message.reply_text(
        "🔬 *Sync Health Check*\n\n⏳ Preparing…",
        parse_mode="Markdown",
    )

    # ── 4. Baseline: record last message ID in destination before the probe ──
    try:
        dest_entity = await client.get_entity(dst)
        recent_dest = await client.get_messages(dest_entity, limit=1)
        baseline_id = recent_dest[0].id if recent_dest else 0
    except Exception as e:
        await status.edit_text(f"❌ Cannot read destination channel:\n`{e}`")
        return

    try:
        src_entity = await client.get_entity(src)
    except Exception as e:
        await status.edit_text(f"❌ Cannot reach source channel:\n`{e}`")
        return

    # ── 5. Send unique probe to source ───────────────────────────────────────
    sentinel  = f"🔬 SYNCTEST-{int(time.time())}"
    probe_msg = None
    found_msg = None

    try:
        probe_msg = await client.send_message(src_entity, sentinel)
    except Exception as e:
        await status.edit_text(f"❌ Failed to send probe to source channel:\n`{e}`")
        return

    await status.edit_text(
        "🔬 *Sync Health Check*\n\n"
        f"📤 Probe sent — waiting for it in destination…\n"
        f"⏳ Timeout: `{_SYNCTEST_TIMEOUT}s`",
        parse_mode="Markdown",
    )

    # ── 6. Poll destination for the sentinel ─────────────────────────────────
    t_start = time.time()
    while time.time() - t_start < _SYNCTEST_TIMEOUT:
        await asyncio.sleep(1.0)
        try:
            # Only look at messages newer than our baseline
            recent = await client.get_messages(
                dest_entity, limit=10, min_id=baseline_id
            )
            for msg in recent:
                if msg.message and sentinel in msg.message:
                    found_msg = msg
                    break
        except Exception:
            pass
        if found_msg:
            break

    elapsed = time.time() - t_start

    # ── 7. Cleanup both channels regardless of outcome ───────────────────────
    if probe_msg:
        try:
            await client.delete_messages(src_entity, [probe_msg.id])
        except Exception:
            pass

    if found_msg:
        try:
            await client.delete_messages(dest_entity, [found_msg.id])
        except Exception:
            pass

    # ── 8. Report ─────────────────────────────────────────────────────────────
    if found_msg:
        await status.edit_text(
            f"✅ *Sync is working!*\n\n"
            f"⚡ Probe arrived in `{elapsed:.1f}s`\n"
            f"📡 Source → Destination pipeline is live.\n\n"
            f"_Test messages deleted from both channels._",
            parse_mode="Markdown",
        )
    else:
        await status.edit_text(
            f"⏱ *Sync test timed out* (`{_SYNCTEST_TIMEOUT}s`)\n\n"
            f"Probe was sent to source but did not appear in destination.\n\n"
            f"*Possible causes:*\n"
            f"• Sync handler crashed — check /status\n"
            f"• Telegram delivery lag — try again in a few seconds\n"
            f"• Source channel bot doesn't have permission to post\n\n"
            f"_Probe deleted from source channel._",
            parse_mode="Markdown",
        )



# ===============================================================================
#  /speed — change copy speed mid-job without restarting
# ===============================================================================

async def speed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /speed — show a speed-picker keyboard.
    If a copy job is running the new delay takes effect on the very next
    message — no restart needed.
    """
    bot_data      = context.bot_data
    copy_task     = bot_data.get("active_copy_task")
    is_running    = bool(copy_task and not copy_task.done())
    current_delay = bot_data.get("active_copy_delay")
    if current_delay is None:
        current_delay = _SPEED_CYCLE[0][1]

    current_label = next(
        (lbl for lbl, dly in _SPEED_CYCLE if abs(dly - current_delay) < 0.001),
        f"{current_delay}s/file",
    )
    buttons = [
        [InlineKeyboardButton(
            ("✅ " if abs(dly - current_delay) < 0.001 else "") + lbl,
            callback_data=f"{_SPEED_CB}{dly}",
        )]
        for lbl, dly in _SPEED_CYCLE
    ]
    status_note = (
        "▶ *Job running* — change takes effect on the very next file."
        if is_running else
        "💤 *No job running* — speed applies to the next /copy."
    )
    await update.message.reply_text(
        f"⚡ *Copy Speed*\n\nCurrent: `{current_label}`\n\n{status_note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def speed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-button handler for /speed picker."""
    query = update.callback_query
    await query.answer()
    try:
        new_delay = float(query.data[len(_SPEED_CB):])
    except ValueError:
        await query.edit_message_text("❌ Invalid speed value.")
        return

    new_label = next(
        (lbl for lbl, dly in _SPEED_CYCLE if abs(dly - new_delay) < 0.001),
        f"{new_delay}s/file",
    )
    bot_data   = context.bot_data
    copy_task  = bot_data.get("active_copy_task")
    is_running = bool(copy_task and not copy_task.done())

    # Write new delay — copy loop reads this on every next message
    bot_data["active_copy_delay"] = new_delay

    buttons = [
        [InlineKeyboardButton(
            ("✅ " if abs(dly - new_delay) < 0.001 else "") + lbl,
            callback_data=f"{_SPEED_CB}{dly}",
        )]
        for lbl, dly in _SPEED_CYCLE
    ]
    effect = (
        "applied immediately to the running job."
        if is_running else
        "will apply to your next /copy job."
    )
    await query.edit_message_text(
        f"⚡ *Speed set to* `{new_label}` — {effect}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  /stats — per-job performance summary from checkpoint files
# ═══════════════════════════════════════════════════════════════════════════════

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /stats — show performance stats pulled from checkpoint files.
    Distinct from /history (duplicate-protection view):
      • Grand totals across all jobs
      • Success rate and flood-wait count per channel pair
      • Live job overlay when a copy is running
    No network calls — reads only local checkpoint files.
    """
    records = _load_all_checkpoints()

    # ── Live job stats (from bot_data, always fresh) ──────────────────────────
    bot_data  = context.bot_data
    copy_task = bot_data.get("active_copy_task")
    live      = (
        bot_data.get("active_copy_stats", {})
        if (copy_task and not copy_task.done()) else None
    )

    if not records and not live:
        await update.message.reply_text(
            "📭 *No stats yet.*\n\n"
            "Run /copy to start copying — progress is recorded here automatically.",
            parse_mode="Markdown",
        )
        return

    # ── Grand totals ──────────────────────────────────────────────────────────
    g_copied  = sum(r.get("copied",      0) for r in records)
    g_skipped = sum(r.get("skipped",     0) for r in records)
    g_failed  = sum(r.get("failed",      0) for r in records)
    g_floods  = sum(r.get("flood_waits", 0) for r in records)
    g_total   = g_copied + g_failed
    g_rate    = f"{g_copied / g_total * 100:.1f}%" if g_total else "n/a"

    n_jobs    = len(records)
    jobs_lbl  = "job" if n_jobs == 1 else "jobs"
    flood_lbl = "wait" if g_floods == 1 else "waits"
    flood_hdr = f"  \u23f3 `{g_floods:,}` flood {flood_lbl}" if g_floods else ""

    lines = [
        "\U0001f4ca *Copy Stats \u2014 All Jobs*\n",
        f"\U0001f4e6 *Grand total across `{n_jobs}` {jobs_lbl}*\n"
        f"   \u2705 `{g_copied:,}` copied  \u23ed `{g_skipped:,}` skipped  "
        f"\u274c `{g_failed:,}` failed{flood_hdr}\n"
        f"   \U0001f3af Success rate: `{g_rate}`",
    ]

    # ── Per-pair rows ─────────────────────────────────────────────────────────
    if records:
        lines.append("\n\U0001f4cb *Per-channel pair:*")

    for i, rec in enumerate(records, 1):
        src_id  = rec.get("source_id", "?")
        dst_id  = rec.get("dest_id",   "?")
        copied  = rec.get("copied",      0)
        skipped = rec.get("skipped",     0)
        failed  = rec.get("failed",      0)
        floods  = rec.get("flood_waits", 0)
        updated = (rec.get("updated_at") or rec.get("started_at") or "\u2014")[:16]
        total   = copied + failed
        rate    = f"{copied / total * 100:.0f}%" if total else "n/a"

        # 10-char progress bar: filled = fraction of (copied / all processed)
        denom  = copied + skipped + failed
        filled = int(10 * copied / denom) if denom else 0
        bar    = "\u2588" * filled + "\u2591" * (10 - filled)

        flood_lbl_p = "wait" if floods == 1 else "waits"
        flood_note  = f"  \u23f3 `{floods}` flood {flood_lbl_p}" if floods else ""

        lines.append(
            f"\n*{i}.* `{src_id}` \u2192 `{dst_id}`\n"
            f"   `{bar}` `{rate}`  \u2705 `{copied:,}` \u23ed `{skipped:,}` \u274c `{failed:,}`{flood_note}\n"
            f"   \U0001f552 {_code(updated)}"
        )

    # ── Live job overlay ──────────────────────────────────────────────────────
    if live:
        lc = live.get("copied",  0)
        ls = live.get("skipped", 0)
        lf = live.get("failed",  0)
        lt = live.get("total",   0)
        total_note = f" / `{lt:,}`" if lt else ""

        flood     = bot_data.get("active_flood_wait")
        flood_live = ""
        if flood:
            remaining = int(flood["until"] - time.time())
            if remaining > 0:
                fm, fs = divmod(remaining, 60)
                flood_live = f"  \u23f3 Flood wait: `{fm}m {fs}s` remaining"

        cur_delay  = bot_data.get("active_copy_delay")
        speed_lbl  = next(
            (lbl for lbl, dly in _SPEED_CYCLE
             if cur_delay is not None and abs(dly - cur_delay) < 0.001),
            "",
        )
        speed_note = f"  \u26a1 {_code(speed_lbl)}" if speed_lbl else ""

        lines.append(
            f"\n\u25b6 *Live job running*\n"
            f"   \u2705 `{lc:,}`{total_note}  \u23ed `{ls:,}`  \u274c `{lf:,}`{speed_note}{flood_live}"
        )

    lines.append("\n_Use /history for duplicate-protection details._")

    # ── Chunk into ≤4000-char messages ────────────────────────────────────────
    pages: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) > 3900 and current:
            pages.append(current)
            current = line
        else:
            current += line
    if current:
        pages.append(current)

    for page in pages:
        await update.message.reply_text(page, parse_mode="Markdown")




# ═══════════════════════════════════════════════════════════════════════════════
#  /setcaption — set a custom suffix appended to every copied file's caption
# ═══════════════════════════════════════════════════════════════════════════════

async def setcaption_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setcaption <text>  — append <text> as a new line to every copied file caption.
    /setcaption off     — remove the suffix (copy captions as-is).
    /setcaption         — show what is currently set.

    The suffix is stored in user_data so it survives bot restarts.
    It is applied AFTER username replacement, so you can combine both:
      CAPTION_REPLACE = "@YourChannel"  (replaces source @usernames)
      /setcaption 📌 Join @YourChannel  (always appended at the bottom)
    """
    args = (context.args or [])
    text = " ".join(args).strip()

    if not text:
        current = context.user_data.get("caption_suffix", config.CAPTION_SUFFIX)
        if current:
            await update.message.reply_text(
                f"📝 *Current caption suffix:*\n`{current}`\n\n"
                "Use `/setcaption off` to remove it, or `/setcaption <text>` to change it.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "📝 *No caption suffix set.*\n\n"
                "Use `/setcaption <text>` to add a line to the bottom of every copied caption.\n"
                "Example: `/setcaption 📌 @YourChannel`",
                parse_mode="Markdown",
            )
        return

    if text.lower() == "off":
        context.user_data.pop("caption_suffix", None)
        await update.message.reply_text(
            "✅ Caption suffix *removed* — captions will be copied as-is.",
            parse_mode="Markdown",
        )
        return

    context.user_data["caption_suffix"] = text
    await update.message.reply_text(
        f"✅ Caption suffix set to:\n`{text}`\n\n"
        "This line will be appended to the bottom of every copied file caption.\n"
        "Takes effect on the next /copy (or /resume).",
        parse_mode="Markdown",
    )



# ═══════════════════════════════════════════════════════════════════════════════
#  /cleancaptions — bulk-edit existing destination channel messages to strip
#  watermark lines (e.g. "FILE ADDED BY GOUTHAM SER ❤️") from their captions.
#  Uses the same STRIP_PATTERNS from config.py as the copy engine does.
# ═══════════════════════════════════════════════════════════════════════════════

_CC_CANCEL_KEY = "cleancaptions_cancel"


async def _run_cleancaptions(client, dest_entity, bot, chat_id, status_msg_id, bot_data):
    """
    Background task: iterate every message in dest_entity and edit any caption
    that contains a line matching config.STRIP_PATTERNS or _WATERMARK_RE.
    Reports progress via Telegram message edits.
    """
    from telethon.errors import (
        FloodWaitError,
        MessageNotModifiedError,
        MessageIdInvalidError,
        ChatAdminRequiredError,
    )
    from userbot.filter_utils import clean_caption

    scanned = edited = skipped = errors = 0
    last_edit = time.time()

    async def _update(done: bool = False):
        nonlocal last_edit
        now = time.time()
        if not done and now - last_edit < 4:
            return
        last_edit = now
        icon = "✅" if done else "🧹"
        suffix = "" if done else "\n\nSend /stopcleaning to cancel."
        try:
            await bot.edit_message_text(
                f"{icon} *Caption Cleaner*\n\n"
                f"📋 Scanned : `{scanned:,}`\n"
                f"✏️  Edited  : `{edited:,}`\n"
                f"⏭  Skipped : `{skipped:,}`\n"
                f"❌ Errors  : `{errors:,}`"
                f"{suffix}",
                chat_id=chat_id,
                message_id=status_msg_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    try:
        async for msg in client.iter_messages(dest_entity, reverse=True):
            # Cancellation check
            if bot_data.get(_CC_CANCEL_KEY):
                bot_data[_CC_CANCEL_KEY] = False
                await _update(done=False)
                try:
                    await bot.edit_message_text(
                        f"🛑 *Caption cleaning cancelled.*\n\n"
                        f"📋 Scanned `{scanned:,}` · ✏️ Edited `{edited:,}`",
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                return

            scanned += 1

            # Only messages with a text/caption are candidates
            raw = msg.message
            if not raw:
                skipped += 1
                await asyncio.sleep(0)
                continue

            # Run through the same cleaner the copy engine uses
            cleaned = clean_caption(raw, replacement="")

            if cleaned == raw.strip():
                skipped += 1
                await asyncio.sleep(0)
                continue

            # Caption changed — attempt to edit the message in-place
            retries = 0
            while retries < 3:
                try:
                    await client.edit_message(
                        dest_entity,
                        msg.id,
                        text=cleaned,
                        parse_mode="md",
                    )
                    edited += 1
                    break
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 2)
                except (MessageNotModifiedError, MessageIdInvalidError):
                    skipped += 1
                    break
                except ChatAdminRequiredError:
                    errors += 1
                    await bot.send_message(
                        chat_id,
                        "❌ *Caption Cleaner stopped:* the userbot doesn't have "
                        "permission to edit messages in the destination channel.\n\n"
                        "Make sure the userbot is an admin with *Edit Messages* permission.",
                        parse_mode="Markdown",
                    )
                    return
                except Exception:
                    retries += 1
                    if retries >= 3:
                        errors += 1
                    await asyncio.sleep(1)

            await _update()
            await asyncio.sleep(0.05)   # gentle pacing — ~20 edits/sec max

    finally:
        bot_data.pop(_CC_CANCEL_KEY, None)
        bot_data["active_cleancaptions_task"] = None

    await _update(done=True)


async def cleancaptions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /cleancaptions — scan the destination channel and strip watermark lines
    (e.g. "FILE ADDED BY GOUTHAM SER ❤️") from the captions of all existing
    messages.  Uses the same STRIP_PATTERNS from config.py as /copy does.

    Progress is shown live in a Telegram message.
    Send /stopcleaning to cancel mid-run.
    """
    bot_data = context.bot_data

    # Only one cleaner at a time
    existing = bot_data.get("active_cleancaptions_task")
    if existing and not existing.done():
        await update.message.reply_text(
            "⚠️ *Caption cleaner is already running.*\n\n"
            "Send /stopcleaning to cancel it first.",
            parse_mode="Markdown",
        )
        return

    if not bridge.is_ready(bot_data):
        await update.message.reply_text(
            _not_ready(bridge.is_locked(bot_data), bridge.is_starting_up(bot_data)), parse_mode="Markdown"
        )
        return

    client = bridge.get_client(bot_data)
    dest   = config.DEST_CHANNEL

    if not dest:
        await update.message.reply_text(
            "❌ *DEST\_CHANNEL not set in config.py.*\n\n"
            "Set it to the channel ID of your destination channel.",
            parse_mode="Markdown",
        )
        return

    try:
        dest_entity = await client.get_entity(dest)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not resolve destination channel:\n`{e}`",
            parse_mode="Markdown",
        )
        return

    status = await update.message.reply_text(
        "🧹 *Caption Cleaner starting…*\n\n"
        "Scanning destination channel for captions to clean.\n"
        "_This may take a while for large channels._\n\n"
        "Send /stopcleaning to cancel.",
        parse_mode="Markdown",
    )

    chat_id = update.effective_chat.id
    bot_data[_CC_CANCEL_KEY] = False

    task = asyncio.create_task(
        _run_cleancaptions(
            client, dest_entity,
            context.application.bot, chat_id, status.message_id,
            bot_data,
        )
    )
    bot_data["active_cleancaptions_task"] = task


async def stopcleaning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /stopcleaning — cancel a running /cleancaptions job.
    """
    bot_data = context.bot_data
    task = bot_data.get("active_cleancaptions_task")
    if task and not task.done():
        bot_data[_CC_CANCEL_KEY] = True
        await update.message.reply_text(
            "🛑 Cancelling caption cleaner… it will stop after the current message.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No caption cleaner is running.",
            parse_mode="Markdown",
        )

def get_extra_handlers() -> list:
    """Standalone command handlers registered outside the conversation."""
    return [
        CommandHandler("status",    status_cmd),
        CommandHandler("stopjob",   stopjob_cmd),
        CommandHandler("resume",    resume_cmd),
        CommandHandler("stopsync",  stopsync_cmd),
        CommandHandler("synctest",  synctest_cmd),
        CommandHandler("listchats", listchats_cmd),
        CommandHandler("history",       copystats_cmd),
        CommandHandler("clearhistory",  clearhistory_cmd),
        CommandHandler("config",        config_cmd),
        CommandHandler("speed",         speed_cmd),
        CommandHandler("stats",         stats_cmd),
        CommandHandler("setcaption",    setcaption_cmd),
        CommandHandler("cleancaptions", cleancaptions_cmd),
        CommandHandler("stopcleaning",  stopcleaning_cmd),
        # Bare callback handlers so these buttons work even when the user
        # is NOT inside the main-menu conversation (e.g. from /status output).
        # The conv's MAIN_MENU handlers take priority when the user IS in that
        # state; these catch all other cases that would otherwise be silently dropped.
        CallbackQueryHandler(resume_callback,       pattern=f"^{_RESUME_CONFIRM}$"),
        CallbackQueryHandler(resume_callback,       pattern=f"^{_RESUME_CANCEL}$"),
        CallbackQueryHandler(status_callback,       pattern="^status_menu$"),
        CallbackQueryHandler(listchats_callback,    pattern="^listchats_menu$"),
        CallbackQueryHandler(clearhistory_callback, pattern="^clrhist"),
        CallbackQueryHandler(speed_callback,        pattern=f"^{_SPEED_CB}"),
        CallbackQueryHandler(ar_cancel_callback,        pattern="^ar_cancel$"),
        CallbackQueryHandler(ar_resume_saved_callback,  pattern="^ar_resume_saved$"),
        CallbackQueryHandler(ar_cancel_saved_callback,  pattern="^ar_cancel_saved$"),
    ]
