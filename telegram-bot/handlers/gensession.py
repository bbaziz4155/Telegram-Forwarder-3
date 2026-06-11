"""
/gensession — generate a fresh Telethon SESSION_STRING from inside the bot.

Flow:
  /gensession
  → sends phone number
  → sends OTP code
  → (if 2FA) sends cloud password
  → bot replies with the SESSION_STRING to copy into env vars

The wizard creates its own temporary TelegramClient (StringSession "")
that is completely separate from the main userbot bridge.  After the
session string is exported the temporary client is disconnected and
all wizard state is cleaned up.

Security note:
  The session string is sent as a Telegram message.  The bot reminds
  the user to copy it, save it as a secret, and delete the message.
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
from telethon import TelegramClient
from telethon.sessions import StringSession

from states import GENSESSION_2FA, GENSESSION_OTP, GENSESSION_PHONE

logger = logging.getLogger(__name__)

# Shown whenever Telegram rejects an auth key because two servers connected
# simultaneously with the same session (common during Railway rolling deploys).
_AUTH_KEY_DUPED_MSG = (
    "❌ *Session conflict — two connections sharing the same key*\n\n"
    "Telegram blocked the connection because the same session string is being used "
    "from two different IPs at once. This usually happens when Railway starts a new "
    "container before fully stopping the old one.\n\n"
    "*Fix — choose one:*\n"
    "① Go to [my.telegram.org](https://my.telegram.org) → *Active Sessions* → "
    "*Terminate all other sessions*, wait 30 s, then /gensession again.\n"
    "② In Railway: manually *stop* the current deployment, run /gensession to get "
    "a fresh session string, paste it as `TELETHON\_SESSION`, then redeploy."
)


_RESEND_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔄 Resend code", callback_data="gs_resend")],
    [InlineKeyboardButton("❌ Cancel",       callback_data="gs_cancel")],
])
_CANCEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("❌ Cancel", callback_data="gs_cancel")],
])

_KEY_CLIENT  = "gs_client"
_KEY_PHONE   = "gs_phone"
_KEY_SENT    = "gs_sent"
_KEY_ATTEMPTS = "gs_attempts"
_KEY_RESENDS  = "gs_resends"


def _menu_kb():
    from handlers.menu import main_menu_keyboard
    return main_menu_keyboard()


def _where_was_code_sent(sent) -> str:
    try:
        type_name = type(sent.type).__name__
    except Exception:
        return "your Telegram"
    if "App" in type_name:
        return (
            "📱 *your Telegram app* (Saved Messages)\n"
            "👉 Open Telegram → tap *Saved Messages* → scroll to the *very bottom* — "
            "use only the *last* code."
        )
    if "Sms" in type_name:
        return "📨 *SMS* to your phone number"
    if "FlashCall" in type_name:
        return "📞 *flash call* — last digits of caller's number are your code"
    if "MissedCall" in type_name:
        return "📞 *missed call* — last digits of caller's number are your code"
    if "Call" in type_name:
        return "📞 *automated phone call*"
    if "Email" in type_name:
        return "📧 *email*"
    return "your Telegram"


async def _cleanup(context: ContextTypes.DEFAULT_TYPE):
    client: TelegramClient | None = context.user_data.pop(_KEY_CLIENT, None)
    if client is not None:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
    for k in (_KEY_PHONE, _KEY_SENT, _KEY_ATTEMPTS, _KEY_RESENDS):
        context.user_data.pop(k, None)


# ── entry ─────────────────────────────────────────────────────────────────────

async def gensession_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id   = os.environ.get("TELEGRAM_API_ID",   "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()

    if not api_id or not api_hash:
        await update.message.reply_text(
            "❌ *Cannot generate session — API credentials missing.*\n\n"
            "Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` as environment variables "
            "in your hosting platform, then try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    try:
        from telethon.errors import AuthKeyDuplicatedError as _AKDE
    except ImportError:
        _AKDE = None
    try:
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
    except Exception as e:
        logger.exception("gensession: could not connect fresh client")
        if _AKDE and isinstance(e, _AKDE):
            await update.message.reply_text(
                _AUTH_KEY_DUPED_MSG,
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(
                f"❌ *Could not connect to Telegram:* `{e}`\n\n"
                "Check `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` and try again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        return ConversationHandler.END

    context.user_data[_KEY_CLIENT]   = client
    context.user_data[_KEY_ATTEMPTS] = 0
    context.user_data[_KEY_RESENDS]  = 0

    await update.message.reply_text(
        "🔑 *Generate Session String*\n\n"
        "This creates a fresh `SESSION_STRING` you can copy into your "
        "hosting platform's environment variables.\n\n"
        "Send your phone number with country code:\n"
        "Example: `+12345678901`",
        parse_mode="Markdown",
        reply_markup=_CANCEL_KB,
    )
    return GENSESSION_PHONE


# ── phone ─────────────────────────────────────────────────────────────────────

async def gensession_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        return GENSESSION_PHONE

    client: TelegramClient = context.user_data.get(_KEY_CLIENT)
    if client is None:
        await update.message.reply_text(
            "❌ Session expired. Use /gensession to start over.",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    if not client.is_connected():
        try:
            from telethon.errors import AuthKeyDuplicatedError as _AKDE2
        except ImportError:
            _AKDE2 = None
        try:
            await client.connect()
        except Exception as e:
            if _AKDE2 and isinstance(e, _AKDE2):
                await update.message.reply_text(
                    _AUTH_KEY_DUPED_MSG,
                    parse_mode="Markdown",
                    reply_markup=_menu_kb(),
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(
                    f"❌ *Reconnect failed:* `{e}`\n\nUse /gensession to try again.",
                    parse_mode="Markdown",
                    reply_markup=_menu_kb(),
                )
            await _cleanup(context)
            return ConversationHandler.END

    try:
        from telethon.errors import AuthKeyDuplicatedError as _AKDE3
    except ImportError:
        _AKDE3 = None
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        if _AKDE3 and isinstance(e, _AKDE3):
            await update.message.reply_text(
                _AUTH_KEY_DUPED_MSG,
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(
                f"❌ Could not send OTP: `{e}`\n\nCheck the number and try again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        await _cleanup(context)
        return ConversationHandler.END

    context.user_data[_KEY_PHONE]    = phone
    context.user_data[_KEY_SENT]     = sent
    context.user_data[_KEY_ATTEMPTS] = 0

    where = _where_was_code_sent(sent)
    await update.message.reply_text(
        f"✅ *Code sent to* {where}\n\n"
        "Enter the 5-digit OTP now.\n"
        "_Tap Resend if you don't receive it within 30 s._",
        parse_mode="Markdown",
        reply_markup=_RESEND_KB,
    )
    return GENSESSION_OTP


# ── resend ────────────────────────────────────────────────────────────────────

async def _do_resend(context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, int]:
    client: TelegramClient = context.user_data.get(_KEY_CLIENT)
    phone  = context.user_data.get(_KEY_PHONE)
    try:
        from telethon.errors import FloodWaitError, AuthKeyDuplicatedError
        sent = await client.send_code_request(phone)
        context.user_data[_KEY_SENT]     = sent
        context.user_data[_KEY_ATTEMPTS] = 0
        return True, 0
    except FloodWaitError as fw:
        return False, fw.seconds
    except Exception as e:
        logger.warning("gensession resend failed: %s", e)
        return False, 0


async def gensession_resend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Sending new code…")

    ok, flood_secs = await _do_resend(context)
    if not ok:
        if flood_secs:
            mins     = flood_secs // 60
            wait_msg = f"{mins}m {flood_secs % 60}s" if mins else f"{flood_secs}s"
            await query.edit_message_text(
                f"⏳ *Telegram is rate-limiting code requests.*\n\n"
                f"Please wait *{wait_msg}* then use /gensession to try again.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
        else:
            await query.edit_message_text(
                "❌ Could not resend. Use /gensession to start over.",
                reply_markup=_menu_kb(),
            )
        await _cleanup(context)
        return ConversationHandler.END

    sent  = context.user_data.get(_KEY_SENT)
    where = _where_was_code_sent(sent) if sent else "your Telegram"
    await query.edit_message_text(
        f"✅ *New code sent to* {where}\n\n"
        "Enter the 5-digit OTP now.",
        parse_mode="Markdown",
        reply_markup=_RESEND_KB,
    )
    return GENSESSION_OTP


# ── OTP ───────────────────────────────────────────────────────────────────────

async def gensession_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code   = update.message.text.strip().replace(" ", "")
    phone  = context.user_data.get(_KEY_PHONE)
    sent   = context.user_data.get(_KEY_SENT)
    client: TelegramClient = context.user_data.get(_KEY_CLIENT)

    if client is None or phone is None or sent is None:
        await update.message.reply_text(
            "❌ Session lost. Use /gensession to start over.",
            reply_markup=_menu_kb(),
        )
        return ConversationHandler.END

    try:
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)

    except PhoneCodeExpiredError:
        resends = context.user_data.get(_KEY_RESENDS, 0) + 1
        context.user_data[_KEY_RESENDS] = resends
        if resends > 2:
            await update.message.reply_text(
                "⚠️ *Code keeps expiring — you may be entering an old one.*\n\n"
                "Open Telegram → *Saved Messages* → use only the *last* code.\n\n"
                "Use /gensession to start fresh.",
                parse_mode="Markdown",
                reply_markup=_menu_kb(),
            )
            await _cleanup(context)
            return ConversationHandler.END

        ok, flood_secs = await _do_resend(context)
        if not ok:
            if flood_secs:
                mins = flood_secs // 60
                wait_msg = f"{mins}m {flood_secs % 60}s" if mins else f"{flood_secs}s"
                await update.message.reply_text(
                    f"⏳ *Rate limited.* Wait *{wait_msg}* then use /gensession.",
                    parse_mode="Markdown",
                    reply_markup=_menu_kb(),
                )
            else:
                await update.message.reply_text(
                    "❌ Auto-resend failed. Use /gensession to start over.",
                    reply_markup=_menu_kb(),
                )
            await _cleanup(context)
            return ConversationHandler.END

        sent  = context.user_data.get(_KEY_SENT)
        where = _where_was_code_sent(sent) if sent else "your Telegram"
        await update.message.reply_text(
            f"⚠️ *Code expired — a fresh one has been sent.*\n\n"
            f"Sent to {where}\n\nEnter the new code below.",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return GENSESSION_OTP

    except PhoneCodeInvalidError:
        attempts = context.user_data.get(_KEY_ATTEMPTS, 0) + 1
        context.user_data[_KEY_ATTEMPTS] = attempts
        if attempts >= 3:
            await update.message.reply_text(
                "❌ Too many wrong codes. Use /gensession to start over.",
                reply_markup=_menu_kb(),
            )
            await _cleanup(context)
            return ConversationHandler.END
        left = 3 - attempts
        await update.message.reply_text(
            f"❌ Wrong code ({left} attempt{'s' if left != 1 else ''} left). Try again:",
            reply_markup=_RESEND_KB,
        )
        return GENSESSION_OTP

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 *Two-step verification is enabled.*\n\n"
            "Send your 2FA cloud password:",
            parse_mode="Markdown",
            reply_markup=_CANCEL_KB,
        )
        return GENSESSION_2FA

    except Exception as e:
        logger.exception("gensession sign_in error")
        await update.message.reply_text(
            f"❌ Sign-in error: `{e}`\n\nUse /gensession to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        await _cleanup(context)
        return ConversationHandler.END

    return await _gensession_success(update, context)


# ── 2FA ───────────────────────────────────────────────────────────────────────

async def gensession_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client: TelegramClient = context.user_data.get(_KEY_CLIENT)

    try:
        from telethon.errors import PasswordHashInvalidError
        await client.sign_in(password=password)

    except PasswordHashInvalidError:
        await update.message.reply_text(
            "❌ Wrong password. Try again:",
            reply_markup=_CANCEL_KB,
        )
        return GENSESSION_2FA

    except Exception as e:
        logger.exception("gensession 2FA error")
        await update.message.reply_text(
            f"❌ 2FA error: `{e}`\n\nUse /gensession to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        await _cleanup(context)
        return ConversationHandler.END

    return await _gensession_success(update, context)


# ── cancel ────────────────────────────────────────────────────────────────────

async def gensession_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _cleanup(context)
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("❌ Session generation cancelled.", reply_markup=_menu_kb())
    else:
        await update.message.reply_text("❌ Session generation cancelled.", reply_markup=_menu_kb())
    return ConversationHandler.END


# ── success ───────────────────────────────────────────────────────────────────

async def _gensession_success(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client: TelegramClient = context.user_data.get(_KEY_CLIENT)
    try:
        me             = await client.get_me()
        session_string = client.session.save()
        name           = me.first_name or ""
        uname          = f"@{me.username}" if me.username else f"id={me.id}"
    except Exception as e:
        logger.exception("gensession: could not export session")
        await update.message.reply_text(
            f"❌ Could not export session: `{e}`\n\nUse /gensession to try again.",
            parse_mode="Markdown",
            reply_markup=_menu_kb(),
        )
        await _cleanup(context)
        return ConversationHandler.END
    finally:
        await _cleanup(context)

    # Send the session string in a monospace block so it's easy to copy
    await update.message.reply_text(
        f"✅ *Logged in as {name} ({uname})*\n\n"
        "Here is your `SESSION_STRING`:\n\n"
        f"`{session_string}`\n\n"
        "📋 *How to use it:*\n"
        "Copy the string above and add it as the `SESSION_STRING` environment "
        "variable in your hosting platform (Railway Variables, etc.).\n\n"
        "⚠️ *Security:* This string gives full access to your Telegram account. "
        "Delete this message after copying, and never share it.",
        parse_mode="Markdown",
        reply_markup=_menu_kb(),
    )
    return ConversationHandler.END


# ── ConversationHandler builder ───────────────────────────────────────────────

def build_gensession_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("gensession", gensession_start)],
        allow_reentry=True,
        states={
            GENSESSION_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gensession_phone),
                CallbackQueryHandler(gensession_cancel, pattern="^gs_cancel$"),
            ],
            GENSESSION_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gensession_otp),
                CallbackQueryHandler(gensession_resend, pattern="^gs_resend$"),
                CallbackQueryHandler(gensession_cancel, pattern="^gs_cancel$"),
            ],
            GENSESSION_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, gensession_2fa),
                CallbackQueryHandler(gensession_cancel, pattern="^gs_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", gensession_cancel),
        ],
        per_chat=False,
        per_user=True,
        per_message=False,
    )
