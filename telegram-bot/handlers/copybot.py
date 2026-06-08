"""
Bot handlers that drive the Telethon userbot from within the PTB Telegram bot:

  /copy     — guided bulk-copy wizard (no forwarded-from tag)
  /dryrun   — scan source and preview what would be copied
  /sync     — start live auto-sync (new messages forwarded instantly)
  /stopsync — stop the running auto-sync
  /status   — show current copy-job progress
  /stopjob  — cancel the running copy job
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
# (label, delay_seconds) — Turbo pushes as fast as Telegram allows; FloodWait handles limits
_SPEED_CYCLE  = [("⚡ Turbo (max speed)", 0.0), ("🚀 Fast (0.05s)", 0.05), ("🐢 Normal (0.35s)", 0.35)]
MIN_EDIT_INTERVAL = 5.0


# ═══════════════════════════════════════════════════════════════════════════════
#  BotProgressNotifier — edits a PTB bot message instead of Saved Messages
# ═══════════════════════════════════════════════════════════════════════════════

class BotProgressNotifier(ProgressNotifier):
    """Sends copy progress by editing a PTB bot message."""

    def __init__(self, bot, chat_id: int, message_id: int, every: int = 100,
                 bot_data: dict = None):
        self.bot          = bot
        self.chat_id      = chat_id
        self.message_id   = message_id
        self.every        = every
        self.bot_data     = bot_data   # written on every tick so /status is always fresh
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
            self.bot_data["active_copy_stats"] = {
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
        "notify_every":        config.NOTIFY_EVERY if mode != "sync" else 0,
        "speed_idx":           0,                      # index into _SPEED_CYCLE
        "rate_delay":          _SPEED_CYCLE[0][1],     # seconds between sends
    }


def _opts_keyboard(opts: dict) -> InlineKeyboardMarkup:
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
        context.user_data["copy_src"]     = config.SOURCE_CHANNEL
        context.user_data["copy_src_raw"] = str(config.SOURCE_CHANNEL)
        context.user_data["copy_dst"]     = config.DEST_CHANNEL
        context.user_data["copy_dst_raw"] = str(config.DEST_CHANNEL)
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
    await update.message.reply_text(
        f"{labels[mode]}\n\n"
        f"Enter the *source* channel ID or @username:\n"
        f"_(e.g. `-1001811670072`)_",
        parse_mode="Markdown",
    )
    return COPY_AWAIT_SRC


# ── Step 1: source ────────────────────────────────────────────────────────────

async def got_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["copy_src"]     = _parse_id(text)
    context.user_data["copy_src_raw"] = text
    await update.message.reply_text(
        "📥 Enter the *destination* channel ID or @username:\n"
        "_(e.g. `-1003563437550`)_",
        parse_mode="Markdown",
    )
    return COPY_AWAIT_DST


# ── Step 2: dest → show options keyboard ─────────────────────────────────────

async def got_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["copy_dst"]     = _parse_id(text)
    context.user_data["copy_dst_raw"] = text

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

    elif data == "copt_start":
        await _launch_job(query, context, opts, src_raw, dst_raw)
        return ConversationHandler.END

    await query.edit_message_text(
        _opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_opts_keyboard(opts),
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
    dst     = context.user_data["copy_dst"]

    mode_tags = {"copy": "▶ Copy", "dryrun": "🔍 Dry Run", "sync": "🔄 Auto-Sync"}
    await query.edit_message_text(
        f"{mode_tags[mode]} started!\n\n"
        f"📡 `{src_raw}` → `{dst_raw}`\n"
        f"_Progress updates will appear below…_",
        parse_mode="Markdown",
    )

    if mode == "dryrun":
        status_msg = await bot.send_message(chat_id, "⏳ Scanning channel…")
        task = asyncio.create_task(
            _run_dryrun(client, src, dst, opts, bot, chat_id, status_msg.message_id,
                        context.bot_data)
        )
        context.bot_data["active_copy_task"] = task

    elif mode == "copy":
        status_msg = await bot.send_message(chat_id, "⏳ Initializing copy…")
        notifier   = BotProgressNotifier(
            bot, chat_id, status_msg.message_id,
            every=opts["notify_every"],
            bot_data=context.bot_data,
        )
        # Persist job to disk BEFORE creating the task so a crash mid-start still saves state
        _ar.save_resume(chat_id, src, dst, opts)
        task = asyncio.create_task(
            _run_copy(client, src, dst, opts, notifier, bot, chat_id, context.bot_data)
        )
        context.bot_data["active_copy_task"]   = task
        context.bot_data["active_status_msg"]  = (chat_id, status_msg.message_id)

    elif mode == "sync":
        task = asyncio.create_task(
            _run_sync(client, src, dst, opts, bot, chat_id, context.bot_data)
        )
        context.bot_data["active_sync_task"] = task
        context.bot_data["active_sync_opts"] = opts   # read by /synctest


# ── Background coroutines ─────────────────────────────────────────────────────

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
    try:
        await copy_channel_files(
            client, src, dst,
            allowed_exts=opts["allowed_exts"],
            caption_replacement=opts["caption_replacement"],
            notify_every=opts["notify_every"],
            skip_text=opts["skip_text"],
            notifier=notifier,
            interactive=False,
            rate_delay=opts.get("rate_delay", 0.05),
        )
    except asyncio.CancelledError:
        # Update the progress message to a final state before sending the cancel notice.
        # Without this, the "Initializing copy…" / last-tick message is left frozen.
        stats = bot_data.get("active_copy_stats", {})
        try:
            await notifier.done(
                stats.get("copied",  0),
                stats.get("skipped", 0),
                stats.get("failed",  0),
                stats.get("total",   0),
            )
        except Exception:
            pass
        try:
            await bot.send_message(chat_id, "⛔ Copy job cancelled.")
        except Exception:
            pass
    except Exception as e:
        logger.exception("Copy job error")
        # Update the progress message so it doesn't stay frozen.
        stats = bot_data.get("active_copy_stats", {})
        try:
            await notifier.done(
                stats.get("copied",  0),
                stats.get("skipped", 0),
                stats.get("failed",  0),
                stats.get("total",   0),
            )
        except Exception:
            pass
        try:
            await bot.send_message(chat_id, f"❌ Copy error: {e}")
        except Exception:
            pass
    finally:
        # Clear resume file — this runs on normal finish OR user /stopjob cancel.
        # It does NOT run if the process is killed, which is exactly when we want
        # the file to survive so the job restarts on the next boot.
        _ar.clear_resume()
        bot_data["active_copy_task"]  = None
        bot_data["active_status_msg"] = None


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
        lines.append("🤖 *Userbot:* 🔑 Not logged in — tap Connect Userbot")
    else:
        lines.append("🤖 *Userbot:* ❌ Not initialised")

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
        lines.append(
            f"\n▶ *Copy job running*\n"
            f"  ✅ Copied: `{c:,}`{total_note}  "
            f"⏭ Skipped: `{s:,}`  ❌ Failed: `{f:,}`"
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
        task.cancel()
        await update.message.reply_text("⛔ Cancelling copy job…")
    else:
        await update.message.reply_text("No copy job is currently running.")


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
            _not_ready(locked),
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
        await update.message.reply_text(_not_ready(locked), parse_mode="Markdown")
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

    await update.message.reply_text(
        confirm + "\n\n" + _opts_text(src_raw, dst_raw, opts),
        parse_mode="Markdown",
        reply_markup=_opts_keyboard(opts),
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
        f"📁 *File filter:* {exts_disp}",
        f"🚫 *Text messages:* {skip_disp}",
        f"🔔 *Notifications:* {notify_disp}",
        f"✂️ *Strip patterns* ({len(patterns)}):\n{pat_txt}",
    ]

    # ── Active sync-job overrides (shown when /sync is running) ──────────────
    sync_opts = context.bot_data.get("active_sync_opts")
    if sync_opts:
        o_repl  = sync_opts.get("caption_replacement", config.CAPTION_REPLACE)
        o_exts  = sync_opts.get("allowed_exts") or set()
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

    lines.append("\n_Edit config.py to change defaults._")

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

async def _auto_resume_start(application, resume: dict) -> None:
    """
    Launch a copy job from persisted auto-resume state (no user interaction).
    Sends a Telegram notification to the original chat_id so the user knows
    the job restarted automatically.
    """
    bot      = application.bot
    bot_data = application.bot_data
    chat_id  = resume["chat_id"]
    src      = resume["src"]
    dst      = resume["dst"]
    opts     = resume["opts"]   # already has allowed_exts as a set (load_resume converts)

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

    try:
        notify_msg = await bot.send_message(
            chat_id,
            "♻️ *Auto-Resume*\n\n"
            "The bot restarted while a copy job was running.\n"
            "Resuming from last checkpoint…\n\n"
            f"📡 `{src}` → `{dst}`\n\n"
            "_Send /stopjob to cancel._",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Auto-resume: could not send notification to chat %s: %s", chat_id, e)
        # Still try to resume even without a notification message
        notify_msg = None

    msg_id = notify_msg.message_id if notify_msg else None

    if msg_id:
        notifier = BotProgressNotifier(
            bot, chat_id, msg_id,
            every=opts.get("notify_every", 100),
            bot_data=bot_data,
        )
    else:
        # No message to edit — fall back to a silent notifier
        notifier = ProgressNotifier(client, every=opts.get("notify_every", 100))

    task = asyncio.create_task(
        _run_copy(client, src, dst, opts, notifier, bot, chat_id, bot_data)
    )
    bot_data["active_copy_task"]  = task
    bot_data["active_status_msg"] = (chat_id, msg_id) if msg_id else None
    logger.info("Auto-resume: copy task created for src=%s dst=%s", src, dst)


async def schedule_auto_resume(application) -> None:
    """
    Called once at startup (via asyncio.create_task in post_init).
    Waits for the userbot to be ready, then restores any interrupted copy job.
    """
    resume = _ar.load_resume()
    if not resume:
        return  # nothing to resume — fast path

    logger.info(
        "Auto-resume: found saved job (src=%s dst=%s chat=%s) — waiting for userbot…",
        resume.get("src"), resume.get("dst"), resume.get("chat_id"),
    )

    # Poll until userbot is connected (up to 90 s; bridge typically connects in < 5 s)
    deadline = asyncio.get_event_loop().time() + 90
    while not bridge.is_ready(application.bot_data):
        if asyncio.get_event_loop().time() > deadline:
            logger.warning(
                "Auto-resume: timed out waiting for userbot after 90 s — aborting."
            )
            # Do NOT clear resume file — let the next restart try again
            return
        await asyncio.sleep(2)

    logger.info("Auto-resume: userbot ready — launching resumed job")
    await _auto_resume_start(application, resume)


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
            _not_ready(bridge.is_locked(bot_data)), parse_mode="Markdown"
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
    src    = config.SOURCE_CHANNEL
    dst    = config.DEST_CHANNEL

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


def get_extra_handlers() -> list:
    """Standalone command handlers registered outside the conversation."""
    return [
        CommandHandler("status",    status_cmd),
        CommandHandler("stopjob",   stopjob_cmd),
        CommandHandler("stopsync",  stopsync_cmd),
        CommandHandler("synctest",  synctest_cmd),
        CommandHandler("listchats", listchats_cmd),
        CommandHandler("history",       copystats_cmd),
        CommandHandler("clearhistory",  clearhistory_cmd),
        CommandHandler("config",        config_cmd),
        # Bare callback handlers so these buttons work even when the user
        # is NOT inside the main-menu conversation (e.g. from /status output).
        # The conv's MAIN_MENU handlers take priority when the user IS in that
        # state; these catch all other cases that would otherwise be silently dropped.
        CallbackQueryHandler(status_callback,       pattern="^status_menu$"),
        CallbackQueryHandler(listchats_callback,    pattern="^listchats_menu$"),
        CallbackQueryHandler(clearhistory_callback, pattern="^clrhist"),
    ]
