"""
In-bot Telethon userbot login wizard.

Flow:
  /login  or  "Connect Userbot" button
  → type phone number
  → type OTP (5-digit code)
  → (if 2FA) type cloud password
  → userbot bridge marked ready immediately

OTP-expired fix
───────────────
Telegram expires a phone_code_hash after ~2 minutes, OR sometimes
invalidates it server-side almost immediately (timing / DC routing
issue). We ALWAYS pass the hash explicitly to sign_in() so Telethon
never falls back to a stale cached one. If PhoneCodeExpiredError is
raised we immediately call send_code_request() again, store the fresh
hash, and ask the user for the new code — treating the whole thing as
a silent auto-resend rather than an error.
"""
from __future__ import annotations

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import userbot_bridge as bridge
from states import LOGIN_2FA, LOGIN_OTP, LOGIN_PHONE

logger = logging.getLogger(__name__)

_RESEND_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔄 Resend code", callback_data="login_resend")],
    [InlineKeyboardButton("❌ Cancel",       callback_data="login_cancel")],
])
_CANCEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ Cancel", callback_data="login_cancel")],
])


def _menu_kb():
    from handlers.menu import main_menu_keyboard
    return main_menu_keyboard()


def _cleanup(context: ContextTypes.DEFAULT_TYPE):
    for k in ("login_phone", "login_sent", "login_otp_attempts", "login_resend_count"):
        context.user_data.pop(k, None)


def _where_was_code_sent(sent) -> str:
    """
    Return a human-readable string telling the user WHERE to find their OTP.

    Telethon's SentCode.type is one of:
      SentCodeTypeApp        → delivered to the Telegram app (Saved Messages)
      SentCodeTypeSms        → delivered as an SMS
      SentCodeTypeCall       → delivered via automated phone call
      SentCodeTypeFlashCall  → delivered via flash call (caller hangs up, last digits = code)
      SentCodeTypeMissedCall → delivered via missed call
      SentCodeTypeEmailCode  → delivered by email

    Knowing this is critical: if it's SentCodeTypeApp the user must open
    Telegram → Saved Messages, NOT look in Service Notifications.
    """
    try:
        type_name = type(sent.type).__name__
    except Exception:
        return "your Telegram"

    if "App" in type_name:
        return (
            "📱 *your Telegram app* (Saved Messages)\n"
            "👉 Open Telegram → tap *Saved Messages* → scroll to the *very bottom* — "
            "each request sends a new message, so the valid code is always the *last* one.\n"
            "_(Not SMS, not Service Notifications — it goes to Saved Messages because "
            "you're already logged into Telegram on this device.)_"
        )
    if "Sms" in type_name:
        return "📨 *SMS* to your phone number"
    if "FlashCall" in type_name:
        return "📞 *flash call* — the last digits of the caller's number are your code"
    if "MissedCall" in type_name:
        return "📞 *missed call* — the last digits of the caller's number are your code"
    if "Call" in type_name:
        return "📞 *automated phone call*"
    if "Email" in type_name:
        return "📧 *email*"
    return "your Telegram"


# ── entry ──────────────────────────────────────────────────────────────────

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    async def _reply(text, **kw):
        if query:
            await query.edit_message_text(text, **kw)
        else:
            await update.message.reply_text(text, **kw)

    if bridge.is_ready(context.bot_data):
        await _reply(
            "✅ Userbot is already connected and ready.",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    client = bridge.get_client(context.bot_data)
    if client is None:
        # Client not created yet — API keys missing or connect_loop hasn't run
        api_id   = os.environ.get("TELEGRAM_API_ID",   "")
        api_hash = os.environ.get("TELEGRAM_API_HASH", "")
        if not api_id or not api_hash:
            await _reply(
                "❌ *Userbot not initialised.*\n\n"
                "Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` environment variables "
                "in your Replit Secrets (or environment variables), then restart the bot.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        else:
            await _reply(
                "⏳ *Userbot is still starting up…*\n\n"
                "The background connection task hasn't run yet. "
                "Please wait a few seconds and try again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        return ConversationHandler.END

    # Client exists — may be connected or disconnected (e.g. bad SESSION_STRING
    # caused connect() to fail).  login_phone() will reconnect if needed.
    await _reply(
        "📱 *Userbot Login*\n\n"
        "Send your phone number with country code:\n"
        "Example: `+12345678901`",
        parse_mode="Markdown",
        reply_markup=_CANCEL_KB,
    )
    return LOGIN_PHONE


# ── phone number ────────────────────────────────────────────────────────────

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    if not phone[1:].replace(" ", "").isdigit() or len(phone) < 8:
        await update.message.reply_text(
            "❌ That doesn't look like a valid phone number.\n"
            "Send it with country code, e.g. `+12345678901`",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_PHONE

    client = bridge.get_client(context.bot_data)

    # Guard: reconnect if the client lost its connection since login_start ran.
    # This happens when connect() previously failed (bad/expired SESSION_STRING)
    # but we still set the client in bot_data so the wizard could proceed.
    if not client.is_connected():
        try:
            await client.connect()
        except Exception as e:
            await update.message.reply_text(
                f"❌ *Cannot reach Telegram:* `{e}`\n\n"
                "Check that `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are correct "
                "in your Replit Secrets (TELEGRAM_API_ID / TELEGRAM_API_HASH), then try /login again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
            return ConversationHandler.END

    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not send OTP: `{e}`\n\nCheck the number and try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    context.user_data["login_phone"]         = phone
    context.user_data["login_sent"]          = sent
    context.user_data["login_otp_attempts"]  = 0
    context.user_data["login_resend_count"]  = 0

    where = _where_was_code_sent(sent)
    await update.message.reply_text(
        f"✅ *Code sent to* {where}\n\n"
        "Enter the 5-digit OTP now.\n"
        "_Tap Resend if you don't receive it within 30 s._",
        parse_mode="Markdown",
        reply_markup=_RESEND_KB,
    )
    return LOGIN_OTP


# ── OTP ─────────────────────────────────────────────────────────────────────

async def _do_resend(phone: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, int]:
    """
    Request a fresh OTP. Returns (success, flood_wait_seconds).
    flood_wait_seconds > 0 means Telegram asked us to wait before retrying.
    """
    client = bridge.get_client(context.bot_data)
    try:
        from telethon.errors import FloodWaitError
        sent = await client.send_code_request(phone)
        context.user_data["login_sent"]         = sent
        context.user_data["login_otp_attempts"] = 0
        return True, 0
    except FloodWaitError as fw:
        logger.warning(f"Resend flood-wait: {fw.seconds}s")
        return False, fw.seconds
    except Exception as e:
        logger.warning(f"Resend failed: {e}")
        return False, 0


async def login_resend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Resend code' button — get a fresh phone_code_hash and ask again."""
    query = update.callback_query
    await query.answer("Sending new code…")

    phone = context.user_data.get("login_phone")
    ok, flood_secs = await _do_resend(phone, context)
    if not ok:
        if flood_secs:
            mins = flood_secs // 60
            wait_msg = f"{mins}m {flood_secs % 60}s" if mins else f"{flood_secs}s"
            await query.edit_message_text(
                f"⏳ *Telegram is rate-limiting code requests.*\n\n"
                f"Please wait *{wait_msg}* then tap /login to try again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        else:
            await query.edit_message_text(
                "❌ Could not resend the code. Use /login to start over.",
                reply_markup=_menu_kb(),
            )
        return ConversationHandler.END

    sent  = context.user_data.get("login_sent")
    where = _where_was_code_sent(sent) if sent else "your Telegram"
    await query.edit_message_text(
        f"✅ *New code sent to* {where}\n\n"
        "Enter the 5-digit OTP now.",
        parse_mode="Markdown",
        reply_markup=_RESEND_KB,
    )
    return LOGIN_OTP


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code   = update.message.text.strip().replace(" ", "")
    phone  = context.user_data.get("login_phone")
    sent   = context.user_data.get("login_sent")
    client = bridge.get_client(context.bot_data)

    try:
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )

        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)

    except PhoneCodeExpiredError:
        resend_count = context.user_data.get("login_resend_count", 0) + 1
        context.user_data["login_resend_count"] = resend_count

        if resend_count > 2:
            _cleanup(context)
            await update.message.reply_text(
                "⚠️ *This keeps happening because you may be entering an old code.*\n\n"
                "Each login request sends a *new* code to Saved Messages.\n"
                "👉 Open Telegram → *Saved Messages* → scroll to the *very bottom* — "
                "use only the *last* code in the chat.\n\n"
                "Use /login to start fresh with a new code.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
            return ConversationHandler.END

        ok, flood_secs = await _do_resend(phone, context)
        if not ok:
            if flood_secs:
                mins = flood_secs // 60
                wait_msg = f"{mins}m {flood_secs % 60}s" if mins else f"{flood_secs}s"
                await update.message.reply_text(
                    f"⏳ *Telegram is rate-limiting code requests.*\n\n"
                    f"Please wait *{wait_msg}* then use /login to try again.",
                    parse_mode="Markdown",
                    reply_markup=_menu_kb(),
                )
            else:
                await update.message.reply_text(
                    "❌ Could not auto-resend the code. Use /login to start over.",
                    reply_markup=_menu_kb(),
                )
            return ConversationHandler.END

        sent  = context.user_data.get("login_sent")
        where = _where_was_code_sent(sent) if sent else "your Telegram"
        await update.message.reply_text(
            f"⚠️ *That code expired — a fresh one has been sent.*\n\n"
            f"Sent to {where}\n\n"
            "Enter the new code below.\n"
            "_Still not working? Use /login to start fresh._",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_OTP

    except PhoneCodeInvalidError:
        attempts = context.user_data.get("login_otp_attempts", 0) + 1
        context.user_data["login_otp_attempts"] = attempts
        if attempts >= 3:
            _cleanup(context)
            await update.message.reply_text(
                "❌ Too many incorrect codes. Use /login to start over.",
                reply_markup=_menu_kb(),
            )
            return ConversationHandler.END
        left = 3 - attempts
        await update.message.reply_text(
            f"❌ Wrong code ({left} attempt{'s' if left != 1 else ''} left). Try again:",
            reply_markup=_RESEND_KB,
        )
        return LOGIN_OTP

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 *Two-step verification is enabled.*\n\n"
            "Send your 2FA cloud password:",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_2FA

    except Exception as e:
        logger.exception("sign_in error")
        _cleanup(context)
        await update.message.reply_text(
            f"❌ Sign-in error: `{e}`\n\nUse /login to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    return await _login_success(update, context)


# ── 2FA password ─────────────────────────────────────────────────────────────

async def login_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client   = bridge.get_client(context.bot_data)

    try:
        from telethon.errors import PasswordHashInvalidError
        await client.sign_in(password=password)

    except PasswordHashInvalidError:
        await update.message.reply_text(
            "❌ Wrong password. Try again:",
            reply_markup=_CANCEL_KB,
        )
        return LOGIN_2FA

    except Exception as e:
        logger.exception("2FA error")
        _cleanup(context)
        await update.message.reply_text(
            f"❌ 2FA error: `{e}`\n\nUse /login to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    return await _login_success(update, context)


# ── cancel ───────────────────────────────────────────────────────────────────

async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cleanup(context)
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "❌ Login cancelled.",
            reply_markup=_menu_kb(),
        )
    else:
        await update.message.reply_text(
            "❌ Login cancelled.",
            reply_markup=_menu_kb(),
        )
    return ConversationHandler.END


# ── success ──────────────────────────────────────────────────────────────────

async def _login_success(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from handlers.menu import main_menu_keyboard
    client = bridge.get_client(context.bot_data)
    me     = await client.get_me()
    name   = me.first_name or ""
    uname  = f"@{me.username}" if me.username else f"id={me.id}"

    context.bot_data["userbot_ready"] = True
    _cleanup(context)

    await update.message.reply_text(
        f"✅ *Logged in as {name} ({uname})!*\n\n"
        "All userbot features (copy, sync, history) are now active.\n\n"
        "Use /menu to get started.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(userbot_ready=True),
    )
    return ConversationHandler.END


# ── ConversationHandler builder ──────────────────────────────────────────────

def build_login_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("login", login_start),
            CallbackQueryHandler(login_start, pattern="^userbot_login$"),
        ],
        allow_reentry=True,
        states={
            LOGIN_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone),
                CallbackQueryHandler(login_cancel, pattern="^login_cancel$"),
            ],
            LOGIN_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp),
                CallbackQueryHandler(login_resend, pattern="^login_resend$"),
                CallbackQueryHandler(login_cancel, pattern="^login_cancel$"),
            ],
            LOGIN_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_2fa),
                CallbackQueryHandler(login_cancel, pattern="^login_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", login_cancel),
        ],
        per_chat=False,
        per_user=True,
        per_message=False,
    )