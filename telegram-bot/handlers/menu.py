from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from states import MAIN_MENU

MAIN_MENU_TEXT = (
    "🤖 *Telegram Forwarder Bot*\n\n"
    "Choose an option below:"
)


def main_menu_keyboard(userbot_ready: bool = False):
    connect_label = (
        "✅ Userbot Connected"
        if userbot_ready
        else "🔑 Connect Userbot"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Forward Rule",   callback_data="add_rule")],
        [InlineKeyboardButton("📋 List Forward Rules", callback_data="list_rules")],
        [InlineKeyboardButton("🗑 Delete Forward Rule", callback_data="delete_rule")],
        [InlineKeyboardButton("📜 Forward History",    callback_data="fwd_history")],
        [InlineKeyboardButton("🚫 Ignore List",        callback_data="ignore_list")],
        [InlineKeyboardButton(connect_label,           callback_data="userbot_login"),
         InlineKeyboardButton("📊 Status",             callback_data="status_menu")],
        [InlineKeyboardButton("📡 List My Chats",      callback_data="listchats_menu")],
        [InlineKeyboardButton("👥 Manage Admins",      callback_data="admin_mgmt")],
        [InlineKeyboardButton("ℹ️ Help",               callback_data="help")],
    ])


def _ready(context) -> bool:
    import userbot_bridge as bridge
    return bridge.is_ready(context.bot_data)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        MAIN_MENU_TEXT,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(_ready(context)),
    )
    return MAIN_MENU


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        MAIN_MENU_TEXT,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(_ready(context)),
    )
    return MAIN_MENU


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Help — Telegram Forwarder Bot*\n\n"
        "*How to use:*\n"
        "1. Add this bot as an *admin* to the destination chat/channel.\n"
        "2. Use *Add Forward Rule* to set up auto-forwarding.\n"
        "3. Tap *🔑 Connect Userbot* (or send /login) to enable copy/sync.\n\n"
        "*Bot forwarding features:*\n"
        "• ➕ *Add Forward Rule* — Forward messages from one chat to another\n"
        "• 📋 *List Rules* — See all active forwarding rules\n"
        "• 🗑 *Delete Rule* — Remove a forwarding rule\n"
        "• 📜 *Forward History* — Forward past messages from a chat\n"
        "• 🚫 *Ignore List* — Chats to skip during bulk operations\n\n"
        "*Userbot copy features (no 'Forwarded from' tag):*\n"
        "• /login — Connect your Telegram account _(do this first!)_\n"
        "• /copy — Bulk-copy files from any channel you're a member of\n"
        "• /dryrun — Preview what would be copied _(nothing sent)_\n"
        "• /sync — Start live auto-sync _(new messages forwarded instantly)_\n"
        "• /stopsync — Stop the running auto-sync\n"
        "• /status — Check current copy job progress\n"
        "• /stopjob — Cancel the running copy job\n"
        "• /resume — Restart an interrupted copy job from where it left off\n\n"
        "*Stats & info commands:*\n"
        "• /stats — Performance summary across all copy jobs\n"
        "• /history — Per-channel duplicate-protection stats\n"
        "• /clearhistory — Delete checkpoints so a pair can be re-copied\n"
        "• /config — Show all active configuration settings\n"
        "• /speed — Change copy speed _(Safe / Normal / Fast / Turbo)_\n"
        "• /listchats — List all channels & groups your userbot can see\n\n"
        "*Caption & watermark tools:*\n"
        "• /setcaption — Append a custom line to every copied caption\n"
        "  `/setcaption 📌 @YourChannel` sets it · `/setcaption off` removes it\n"
        "• /cleancaptions — Scan destination channel and strip watermark lines\n"
        "• /stopcleaning — Cancel a running /cleancaptions job\n\n"
        "*Admin management:*\n"
        "• 👥 *Manage Admins* — Add or remove users who can access this bot\n\n"
        "*Session management:*\n"
        "• /gensession — Generate a fresh `SESSION_STRING` in-chat\n"
        "• /deletesession — Permanently revoke the active session\n\n"
        "*Important:* The bot must be an admin in destination chats.\n"
        "For /copy and /sync, you only need to be a *member* of the source channel."
    )
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
            ),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(_ready(context)),
        )
    return MAIN_MENU


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.split()[0] if update.message.text else "that"
    await update.message.reply_text(
        f"❓ `{cmd}` is not a valid command.\n\n"
        "Here's what I can do — tap a button or send a command:",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(_ready(context)),
    )


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()

    greetings = {"hi", "hello", "hey", "hii", "helo", "hiiii", "yo", "sup"}
    thanks    = {"thanks", "thank you", "ty", "thx", "ok", "okay", "k", "done", "cool", "nice"}
    bye       = {"bye", "goodbye", "cya", "see you", "gn", "good night"}

    if text in greetings:
        reply = "👋 Hey! I'm your Telegram Forwarder Bot. Use /menu to see all options."
    elif text in thanks:
        reply = "✅ You're welcome! Let me know if you need anything else — /menu"
    elif text in bye:
        reply = "👋 Goodbye! The bot keeps running in the background."
    else:
        reply = (
            "🤖 I didn't understand that.\n\n"
            "I only respond to commands. Try one of these:"
        )

    await update.message.reply_text(
        reply,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(_ready(context)),
    )
