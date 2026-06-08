from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
import database as db
from states import MAIN_MENU, ADD_RULE_SOURCE, ADD_RULE_DEST, ADD_RULE_CONFIRM, DELETE_RULE_SELECT

async def add_rule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ *Add Forward Rule*\n\n"
        "Step 1/2: Send me the *source chat ID* (the chat to forward FROM).\n\n"
        "💡 To get a chat ID:\n"
        "• Forward any message from that chat to @userinfobot\n"
        "• Or add @RawDataBot to the chat and it will show the ID\n"
        "• For channels: the ID usually starts with -100\n\n"
        "Send the chat ID now, or /cancel to go back.",
        parse_mode="Markdown"
    )
    return ADD_RULE_SOURCE

async def add_rule_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Please send a number (e.g. -1001234567890).")
        return ADD_RULE_SOURCE

    # Try to get chat info
    try:
        chat = await context.bot.get_chat(chat_id)
        chat_name = chat.title or chat.username or chat.first_name or str(chat_id)
    except Exception:
        chat_name = str(chat_id)

    context.user_data["rule_source_id"] = chat_id
    context.user_data["rule_source_name"] = chat_name

    await update.message.reply_text(
        f"✅ Source chat: *{chat_name}* (`{chat_id}`)\n\n"
        "Step 2/2: Now send me the *destination chat ID* (the chat to forward TO).\n\n"
        "Send the chat ID, or /cancel to go back.",
        parse_mode="Markdown"
    )
    return ADD_RULE_DEST

async def add_rule_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid chat ID. Please send a number.")
        return ADD_RULE_DEST

    try:
        chat = await context.bot.get_chat(chat_id)
        chat_name = chat.title or chat.username or chat.first_name or str(chat_id)
    except Exception:
        chat_name = str(chat_id)

    context.user_data["rule_dest_id"] = chat_id
    context.user_data["rule_dest_name"] = chat_name

    source_name = context.user_data["rule_source_name"]
    source_id = context.user_data["rule_source_id"]

    await update.message.reply_text(
        f"📋 *Confirm Forward Rule*\n\n"
        f"From: *{source_name}* (`{source_id}`)\n"
        f"To: *{chat_name}* (`{chat_id}`)\n\n"
        "Confirm this rule?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data="rule_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="menu"),
            ]
        ])
    )
    return ADD_RULE_CONFIRM

async def add_rule_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    source_id = context.user_data["rule_source_id"]
    source_name = context.user_data["rule_source_name"]
    dest_id = context.user_data["rule_dest_id"]
    dest_name = context.user_data["rule_dest_name"]

    rule_id = await db.add_rule(user_id, source_id, source_name, dest_id, dest_name)

    # Register the new rule in the live forwarder (include user_id for ignore-list checks)
    rules = context.bot_data.setdefault("forward_rules", {})
    key = (source_id, dest_id)
    if key not in rules:
        rules[key] = {
            "rule_id":     rule_id,
            "user_id":     user_id,
            "source_name": source_name,
            "dest_name":   dest_name,
        }

    await query.edit_message_text(
        f"✅ *Forward rule created!*\n\n"
        f"Messages from *{source_name}* will now be forwarded to *{dest_name}*.\n\n"
        f"Rule ID: `{rule_id}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]])
    )
    return MAIN_MENU

async def list_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    rules = await db.get_rules(user_id)

    if not rules:
        text = "📋 *Forward Rules*\n\nNo active rules. Use *Add Forward Rule* to create one."
    else:
        lines = ["📋 *Active Forward Rules*\n"]
        for r in rules:
            lines.append(
                f"• `#{r['id']}` {r['source_chat_name']} → {r['dest_chat_name']}"
            )
        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu")]])
    )
    return MAIN_MENU

async def delete_rule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    rules = await db.get_rules(user_id)

    if not rules:
        await query.edit_message_text(
            "🗑 *Delete Rule*\n\nNo active rules to delete.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu")]])
        )
        return MAIN_MENU

    buttons = []
    for r in rules:
        label = f"#{r['id']}: {r['source_chat_name']} → {r['dest_chat_name']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"del_rule_{r['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu")])

    await query.edit_message_text(
        "🗑 *Delete Forward Rule*\n\nSelect a rule to delete:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return DELETE_RULE_SELECT

async def delete_rule_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    rule_id = int(query.data.replace("del_rule_", ""))
    user_id = query.from_user.id

    deleted = await db.delete_rule(rule_id, user_id)

    # Remove from live rules
    rules = context.bot_data.get("forward_rules", {})
    context.bot_data["forward_rules"] = {
        k: v for k, v in rules.items() if v.get("rule_id") != rule_id
    }

    if deleted:
        text = f"✅ Rule `#{rule_id}` deleted successfully."
    else:
        text = "❌ Rule not found or you don't have permission."

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]])
    )
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")]])
    )
    return MAIN_MENU
