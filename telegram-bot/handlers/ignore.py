from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import database as db
from states import MAIN_MENU, IGNORE_ADD_CHAT, IGNORE_REMOVE_SELECT


def _safe(name: str) -> str:
    """Wrap a chat name in backticks for safe Markdown V1 rendering.
    Backtick spans are immune to *, _, [ so any name renders correctly."""
    return "`" + str(name).replace("`", "'") + "`"


async def ignore_list_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    items = await db.get_ignore_list(user_id)

    lines = ["🚫 *Ignore List*\n"]
    if items:
        for item in items:
            lines.append(f"• `#{item['id']}` {_safe(item['chat_name'])} (`{item['chat_id']}`)")
    else:
        lines.append("No chats in ignore list.")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add to Ignore", callback_data="ignore_add")],
            [InlineKeyboardButton("➖ Remove from Ignore", callback_data="ignore_remove")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu")],
        ])
    )
    return MAIN_MENU

async def ignore_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🚫 *Add to Ignore List*\n\n"
        "Send the chat ID to add to the ignore list.\n"
        "The live forwarder will skip messages from ignored chats.",
        parse_mode="Markdown"
    )
    return IGNORE_ADD_CHAT

async def ignore_add_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Please send a number.")
        return IGNORE_ADD_CHAT

    try:
        chat = await context.bot.get_chat(chat_id)
        chat_name = chat.title or chat.username or chat.first_name or str(chat_id)
    except Exception:
        chat_name = str(chat_id)

    user_id = update.message.from_user.id
    added = await db.add_ignore(user_id, chat_id, chat_name)

    if added:
        # Keep in-memory ignore_map in sync so live forwarder respects it immediately
        ignore_map = context.bot_data.setdefault("ignore_map", {})
        ignore_map.setdefault(user_id, set()).add(chat_id)
        reply_text = f"✅ {_safe(chat_name)} added to ignore list."
    else:
        reply_text = f"⚠️ {_safe(chat_name)} is already in your ignore list."

    await update.message.reply_text(
        reply_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]])
    )
    return MAIN_MENU

async def ignore_remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    items = await db.get_ignore_list(user_id)

    if not items:
        await query.edit_message_text(
            "🚫 Ignore list is empty.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="ignore_list")]])
        )
        return MAIN_MENU

    buttons = []
    for item in items:
        name = item['chat_name'] or str(item['chat_id'])
        # Button labels are plain text — no parse_mode, no escaping needed
        buttons.append([InlineKeyboardButton(
            f"#{item['id']}: {name[:30]}",
            callback_data=f"rm_ignore_{item['id']}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ignore_list")])

    await query.edit_message_text(
        "➖ *Remove from Ignore List*\n\nSelect a chat to remove:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return IGNORE_REMOVE_SELECT

async def ignore_remove_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ignore_id = int(query.data.replace("rm_ignore_", ""))
    user_id = query.from_user.id

    # Fetch the chat_id before deleting so we can remove it from the in-memory map
    items = await db.get_ignore_list(user_id)
    chat_id_to_remove = next(
        (item["chat_id"] for item in items if item["id"] == ignore_id), None
    )

    removed = await db.remove_ignore(ignore_id, user_id)

    if removed:
        # Keep in-memory ignore_map in sync so live forwarder respects it immediately
        if chat_id_to_remove is not None:
            ignore_map = context.bot_data.get("ignore_map", {})
            user_set = ignore_map.get(user_id, set())
            user_set.discard(chat_id_to_remove)
        text = "✅ Removed from ignore list."
    else:
        text = "❌ Item not found."

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]])
    )
    return MAIN_MENU