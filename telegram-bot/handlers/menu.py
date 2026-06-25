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
        [InlineKeyboardButton("📡 Add Auto-Forward",   callback_data="add_rule")],
        [InlineKeyboardButton("📋 My Auto-Forwards",   callback_data="list_rules")],
        [InlineKeyboardButton("🗑 Remove Auto-Forward", callback_data="delete_rule")],
        [InlineKeyboardButton("📜 Forward History",    callback_data="fwd_history")],
        [InlineKeyboardButton("🚫 Ignore List",        callback_data="ignore_list")],
        [InlineKeyboardButton(connect_label,           callback_data="userbot_login"),
         InlineKeyboardButton("📊 Status",             callback_data="status_menu")],
        [InlineKeyboardButton("📡 List My Chats",      callback_data="listchats_menu")],
        [InlineKeyboardButton("👥 Manage Admins",      callback_data="admin_mgmt")],
        [InlineKeyboardButton("ℹ️ Help",               callback_data="help"),
         InlineKeyboardButton("📋 Commands",           callback_data="commands")],
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
        "2. Use *Add Auto-Forward* to link a source and destination channel.\n"
        "3. Tap *🔑 Connect Userbot* (or send /login) to enable copy/sync.\n\n"
        "*Auto-Forward (real-time):*\n"
        "• 📡 *Add Auto-Forward* — Link two channels: everything in the source is instantly sent to the destination\n"
        "• 📋 *My Auto-Forwards* — See all your active channel pairs\n"
        "• 🗑 *Remove Auto-Forward* — Delete a channel pair\n"
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
        "*Channel defaults:*\n"
        "• /setsource — Set default source channel\n"
        "• /setdest — Set default destination channel\n"
        "• /channels — Show current source & destination\n\n"
        "*Admin management:*\n"
        "• 👥 *Manage Admins* — Add or remove users who can access this bot\n\n"
        "*Session management:*\n"
        "• /gensession — Generate a fresh `SESSION_STRING` in-chat\n"
        "• /deletesession — Permanently revoke the active session\n\n"
        "*Important:* The bot must be an admin in destination chats.\n"
        "For /copy and /sync, you only need to be a *member* of the source channel."
    )
    back_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
    )
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_markup)
    else:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(_ready(context)),
        )
    return MAIN_MENU


async def commands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 *All Commands*\n"
        "_(Admin access only)_\n\n"

        "🏠 *Navigation*\n"
        "`/start` or `/menu` — Open the main menu\n"
        "`/help` — Full feature guide\n"
        "`/cancel` — Cancel current wizard\n\n"

        "🔑 *Session & Auth*\n"
        "`/login` — Connect your Telegram account\n"
        "`/gensession` — Generate SESSION\\_STRING in-chat\n"
        "`/deletesession` — Revoke the active session\n\n"

        "📡 *Channel Defaults*\n"
        "`/setsource <id>` — Set default source channel\n"
        "`/setdest <id>` — Set default destination channel\n"
        "`/channels` — Show current source & destination\n\n"

        "📦 *Copy Jobs*\n"
        "`/copy` — Bulk copy files (no forward tag)\n"
        "`/dryrun` — Preview copy without sending anything\n"
        "`/resume` — Resume an interrupted copy job\n"
        "`/status` — Check copy job progress\n"
        "`/stopjob` — Cancel the running copy job\n\n"

        "🔄 *Sync*\n"
        "`/sync` — Start live auto-sync (new messages)\n"
        "`/stopsync` — Stop the auto-sync\n"
        "`/synctest` — Test sync connection\n\n"

        "✏️ *Captions*\n"
        "`/setcaption <text>` — Set caption suffix · `/setcaption off` removes it\n"
        "`/previewcaption` — Preview caption after stripping\n"
        "`/strippatterns` — Manage caption strip patterns\n"
        "`/striptest` — Test a pattern on sample text\n"
        "`/cleancaptions` — Strip watermarks from destination channel\n"
        "`/stopcleaning` — Cancel a running cleancaptions job\n\n"

        "🗑 *Maintenance*\n"
        "`/purgedups` — Delete duplicate files in destination\n"
        "`/clearhistory` — Clear copy job checkpoints\n\n"

        "📊 *Stats & Info*\n"
        "`/stats` — Dedup statistics across all jobs\n"
        "`/history` — Per-channel copy history\n"
        "`/config` — Show all active bot settings\n"
        "`/speed` — Change copy speed (Safe/Normal/Fast/Turbo)\n"
        "`/listchats` — List all chats your userbot can see\n"
    )
    back_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]
    )
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=back_markup)
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
