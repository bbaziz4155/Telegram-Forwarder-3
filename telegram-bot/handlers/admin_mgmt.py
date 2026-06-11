import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
import database as db
import config
from states import ADMIN_MGMT, ADMIN_AWAIT_ID

logger = logging.getLogger(__name__)


async def _panel_text_markup() -> tuple:
    admins = await db.list_admins()
    lines = [
        "👥 *Admin Management*\n",
        f"👑 Owner: `{config.OWNER_ID}`\n",
    ]
    if admins:
        lines.append("*Additional Admins:*")
        for a in admins:
            name = f"@{a['username']}" if a.get("username") else str(a["user_id"])
            lines.append(f"• {name}  (`{a['user_id']}`)")
    else:
        lines.append("_No additional admins yet._")

    text = "\n".join(lines)

    rows = []
    for a in admins:
        label = f"❌ Remove {('@' + a['username']) if a.get('username') else a['user_id']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"admin_rm_{a['user_id']}")])

    rows.append([
        InlineKeyboardButton("➕ Add Admin", callback_data="admin_add"),
        InlineKeyboardButton("⬅️ Back",     callback_data="menu"),
    ])
    return text, InlineKeyboardMarkup(rows)


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text, markup = await _panel_text_markup()
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    return ADMIN_MGMT


async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ *Add Admin*\n\n"
        "Send me the Telegram *user ID* of the person to add as admin.\n\n"
        "_Tip: They can get their ID by messaging @userinfobot_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_mgmt")]
        ]),
    )
    return ADMIN_AWAIT_ID


async def admin_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    try:
        new_id = int(raw)
    except ValueError:
        await update.message.reply_text(
            "❌ That's not a valid user ID. Please send a number like `123456789`.",
            parse_mode="Markdown",
        )
        return ADMIN_AWAIT_ID

    if new_id == config.OWNER_ID:
        await update.message.reply_text("👑 That's the owner — already has full access.")
    else:
        username = None
        try:
            member = await context.bot.get_chat(new_id)
            username = getattr(member, "username", None)
        except Exception:
            pass

        added = await db.add_admin(new_id, username=username, added_by=update.effective_user.id)
        if added:
            context.bot_data.setdefault("admin_ids", set()).add(new_id)
            display = f"@{username}" if username else f"`{new_id}`"
            await update.message.reply_text(
                f"✅ {display} added as admin.", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"ℹ️ User `{new_id}` is already an admin.", parse_mode="Markdown"
            )

    text, markup = await _panel_text_markup()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    return ADMIN_MGMT


async def admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        target_id = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        await query.answer("Invalid selection.", show_alert=True)
        return ADMIN_MGMT

    if target_id == config.OWNER_ID:
        await query.answer("Cannot remove the owner.", show_alert=True)
        return ADMIN_MGMT

    removed = await db.remove_admin(target_id)
    if removed:
        context.bot_data.get("admin_ids", set()).discard(target_id)
        await query.answer(f"Removed {target_id}")
    else:
        await query.answer("Admin not found.")

    text, markup = await _panel_text_markup()
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    return ADMIN_MGMT


async def _back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exit admin conv and show the main menu inline keyboard."""
    from handlers.menu import MAIN_MENU_TEXT, main_menu_keyboard
    import userbot_bridge as bridge
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        MAIN_MENU_TEXT,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(bridge.is_ready(context.bot_data)),
    )
    return ConversationHandler.END


async def _cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """End the admin conv on /cancel."""
    return ConversationHandler.END


def build_admin_conv():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_menu, pattern="^admin_mgmt$"),
        ],
        states={
            ADMIN_MGMT: [
                CallbackQueryHandler(admin_add_start, pattern="^admin_add$"),
                CallbackQueryHandler(admin_remove,    pattern=r"^admin_rm_\d+$"),
                CallbackQueryHandler(_back_to_menu,   pattern="^menu$"),
            ],
            ADMIN_AWAIT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_id),
                CallbackQueryHandler(admin_menu,    pattern="^admin_mgmt$"),
                CallbackQueryHandler(_back_to_menu, pattern="^menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel_conv),
            CommandHandler("start",  _cancel_conv),
        ],
        allow_reentry=True,
        per_chat=False,
        per_user=True,
        per_message=False,
    )
