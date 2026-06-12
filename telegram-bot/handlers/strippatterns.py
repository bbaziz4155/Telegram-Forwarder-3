"""
/strippatterns — manage caption watermark strip patterns directly from Telegram.

Built-in patterns (hardcoded in config.STRIP_PATTERNS) are shown read-only.
Custom patterns are stored in data/strip_patterns.json and can be added
or removed without touching code or redeploying on Railway.

After any change, filter_utils.reload_strip_patterns() rebuilds the live
regex so the next /copy or /cleancaptions run uses the updated list.
"""
import re
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config as _cfg
from userbot.filter_utils import (
    load_custom_patterns,
    save_custom_patterns,
    reload_strip_patterns,
    clean_caption,
)
from states import STRIP_MGMT, STRIP_AWAIT_ADD, STRIP_AWAIT_TEST

logger = logging.getLogger(__name__)

# ── Callback data constants ───────────────────────────────────────────────────
_CB_ADD    = "sp_add"
_CB_TEST   = "sp_test"
_CB_BACK   = "sp_back"
_CB_RM_PFX = "sp_rm_"   # + str(index) within custom list

# All MarkdownV2 special chars that must be escaped outside code spans
_MV2_SPECIAL = r"\_*[]()~`>#+-=|{}.!"


def _esc_mv2(text: str) -> str:
    """Escape all MarkdownV2 special characters in plain text."""
    for ch in _MV2_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text


def _esc_code(text: str) -> str:
    """Escape text for use inside a MarkdownV2 inline code span (backticks)."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _builtin_patterns() -> list:
    return list(getattr(_cfg, "STRIP_PATTERNS", []))


def _make_menu_text() -> str:
    builtin = _builtin_patterns()
    custom  = load_custom_patterns()

    lines = ["✂️ *Caption Strip Patterns*\n"]
    lines.append(_esc_mv2("These patterns remove matching lines from captions during /copy and /cleancaptions.") + "\n")

    if builtin:
        lines.append("*🔒 Built\\-in \\(read\\-only\\):*")
        for p in builtin:
            lines.append(f"  • `{_esc_code(p)}`")
        lines.append("")

    if custom:
        lines.append("*✏️ Custom \\(your additions\\):*")
        for i, p in enumerate(custom):
            lines.append(f"  {i+1}\\. `{_esc_code(p)}`")
    else:
        lines.append("_No custom patterns yet\\._")

    total = len(builtin) + len(custom)
    lines.append(
        f"\n*Total active:* {total} pattern\\(s\\)\n\n"
        "Use the buttons below to add a pattern, remove a custom one, or test "
        "whether a caption line would be stripped\\."
    )
    return "\n".join(lines)


def _make_menu_markup() -> InlineKeyboardMarkup:
    custom = load_custom_patterns()
    rows   = []

    for i, p in enumerate(custom[:10]):
        label = p[:28] + "…" if len(p) > 28 else p
        rows.append([InlineKeyboardButton(
            f"❌ Remove: {label}", callback_data=f"{_CB_RM_PFX}{i}"
        )])

    rows.append([
        InlineKeyboardButton("➕ Add Pattern", callback_data=_CB_ADD),
        InlineKeyboardButton("🧪 Test Text",   callback_data=_CB_TEST),
    ])
    rows.append([InlineKeyboardButton("🔙 Back to Menu", callback_data=_CB_BACK)])
    return InlineKeyboardMarkup(rows)


# ── Entry point ───────────────────────────────────────────────────────────────

async def strippatterns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = await update.message.reply_text(
        _make_menu_text(),
        parse_mode="MarkdownV2",
        reply_markup=_make_menu_markup(),
    )
    context.user_data["sp_msg_id"] = msg.message_id
    return STRIP_MGMT


# ── STRIP_MGMT state callbacks ────────────────────────────────────────────────

async def _refresh_menu(query, context) -> int:
    try:
        await query.edit_message_text(
            _make_menu_text(),
            parse_mode="MarkdownV2",
            reply_markup=_make_menu_markup(),
        )
    except Exception:
        pass
    return STRIP_MGMT


async def sp_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    # ── Remove a custom pattern ───────────────────────────────────────────────
    if data.startswith(_CB_RM_PFX):
        idx    = int(data[len(_CB_RM_PFX):])
        custom = load_custom_patterns()
        if 0 <= idx < len(custom):
            removed = custom.pop(idx)
            save_custom_patterns(custom)
            reload_strip_patterns()
            await query.answer(f"Removed: {removed[:40]}", show_alert=False)
            logger.info("Removed strip pattern #%d: %r", idx, removed)
        return await _refresh_menu(query, context)

    # ── Add pattern ───────────────────────────────────────────────────────────
    if data == _CB_ADD:
        await query.edit_message_text(
            "✂️ *Add Strip Pattern*\n\n"
            "Send the text or regex that should be stripped from captions\\.\n\n"
            "*Examples:*\n"
            "• `file added by john` — removes any line containing this\n"
            "• `join.*@mychannel` — regex: removes join\\-promo lines\n"
            "• `latest movies` — case\\-insensitive substring match\n\n"
            "_Send /cancel to go back without saving\\._",
            parse_mode="MarkdownV2",
        )
        return STRIP_AWAIT_ADD

    # ── Test text ─────────────────────────────────────────────────────────────
    if data == _CB_TEST:
        await query.edit_message_text(
            "🧪 *Test Caption Stripping*\n\n"
            "Send a sample caption \\(one or more lines\\) and I'll show you "
            "exactly what it looks like after all patterns are applied\\.\n\n"
            "_Send /cancel to go back\\._",
            parse_mode="MarkdownV2",
        )
        return STRIP_AWAIT_TEST

    # ── Back to main menu ─────────────────────────────────────────────────────
    # FIX: callback query updates have no update.message — edit inline and end conv.
    if data == _CB_BACK:
        try:
            await query.edit_message_text(
                "↩️ Returned to menu\\. Send /start to open the main menu\\.",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass
        return ConversationHandler.END

    return STRIP_MGMT


# ── STRIP_AWAIT_ADD state ─────────────────────────────────────────────────────

async def sp_got_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("⚠️ Pattern cannot be empty. Send /cancel to abort.")
        return STRIP_AWAIT_ADD

    # Validate that it's a legal regex
    try:
        re.compile(raw, re.IGNORECASE)
    except re.error as e:
        await update.message.reply_text(
            f"⚠️ *Invalid regex:* `{e}`\n\n"
            "Fix the pattern and send it again, or send /cancel to abort.",
            parse_mode="Markdown",
        )
        return STRIP_AWAIT_ADD

    # Guard against duplicates
    custom  = load_custom_patterns()
    builtin = _builtin_patterns()
    if raw in custom or raw in builtin:
        await update.message.reply_text(
            "ℹ️ That pattern already exists. Send another or /cancel.",
        )
        return STRIP_AWAIT_ADD

    custom.append(raw)
    save_custom_patterns(custom)
    total = reload_strip_patterns()
    logger.info("Added strip pattern: %r (total=%d)", raw, total)

    # Show fresh menu with confirmation
    await update.message.reply_text(
        f"✅ *Pattern added!*\n\n`{raw}`\n\nTotal active patterns: {total}",
        parse_mode="Markdown",
    )
    menu_msg = await update.message.reply_text(
        _make_menu_text(),
        parse_mode="MarkdownV2",
        reply_markup=_make_menu_markup(),
    )
    context.user_data["sp_msg_id"] = menu_msg.message_id
    return STRIP_MGMT


# ── STRIP_AWAIT_TEST state ────────────────────────────────────────────────────

async def sp_got_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text or ""
    if not raw.strip():
        await update.message.reply_text("⚠️ Please send some caption text to test.")
        return STRIP_AWAIT_TEST

    cleaned = clean_caption(raw, replacement="")

    # FIX: fully escape user input for MarkdownV2 code blocks
    raw_esc     = _esc_code(raw)
    cleaned_esc = _esc_code(cleaned) if cleaned else "(empty — all lines stripped)"

    if cleaned == raw.strip():
        result_text = (
            "🟡 *No patterns matched* — caption would be unchanged\\.\n\n"
            f"*Input:*\n```\n{raw_esc}\n```"
        )
    else:
        result_text = (
            "✅ *Patterns matched\\!* Here's what the caption looks like after stripping:\n\n"
            f"*Before:*\n```\n{raw_esc}\n```\n\n"
            f"*After:*\n```\n{cleaned_esc}\n```"
        )

    back_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Patterns", callback_data="sp_return_menu"),
    ]])
    await update.message.reply_text(
        result_text,
        parse_mode="MarkdownV2",
        reply_markup=back_markup,
    )
    return STRIP_MGMT


# ── Return-to-menu callback (from test result back button) ────────────────────

async def sp_return_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _refresh_menu(query, context)


# ── /cancel fallback ──────────────────────────────────────────────────────────

async def sp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "↩️ Cancelled. Use /strippatterns to manage patterns again.",
    )
    return ConversationHandler.END


# ── Handler factory ───────────────────────────────────────────────────────────

def build_strippatterns_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("strippatterns", strippatterns_cmd)],
        states={
            STRIP_MGMT: [
                CallbackQueryHandler(sp_menu_callback,        pattern=f"^(sp_add|sp_test|sp_back|{_CB_RM_PFX}\\d+)$"),
                CallbackQueryHandler(sp_return_menu_callback, pattern="^sp_return_menu$"),
            ],
            STRIP_AWAIT_ADD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sp_got_pattern),
            ],
            STRIP_AWAIT_TEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sp_got_test),
                CallbackQueryHandler(sp_return_menu_callback, pattern="^sp_return_menu$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", sp_cancel)],
        per_chat=False,
        per_user=True,
        per_message=False,
        allow_reentry=True,
    )
