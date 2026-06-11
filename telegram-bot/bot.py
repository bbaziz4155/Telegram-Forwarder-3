import asyncio
import logging
import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    PicklePersistence,
    PersistenceInput,
    TypeHandler,
    filters,
)
from telegram.ext import ApplicationHandlerStop
import database as db
import forwarder
import userbot_bridge
import config
from handlers import menu as menu_handler
from handlers import rules as rules_handler
from handlers import history as history_handler
from handlers import ignore as ignore_handler
from handlers import copybot as copybot_handler
from handlers import login as login_handler
from handlers import preview as preview_handler
from handlers import gensession as gensession_handler
from handlers import deletesession as deletesession_handler
from handlers import admin_mgmt as admin_mgmt_handler
from states import (
    MAIN_MENU,
    ADD_RULE_SOURCE,
    ADD_RULE_DEST,
    ADD_RULE_CONFIRM,
    DELETE_RULE_SELECT,
    IGNORE_ADD_CHAT,
    IGNORE_REMOVE_SELECT,
    FORWARD_HISTORY_SOURCE,
    FORWARD_HISTORY_DEST,
    FORWARD_HISTORY_LIMIT,
)

logger = logging.getLogger(__name__)


async def _admin_gate(update: Update, context) -> None:
    """
    Runs before all other handlers (group -1).
    Blocks any user who is not the owner or an approved admin.
    If OWNER_ID is 0 (not configured), lets everyone through with a warning.
    """
    if config.OWNER_ID == 0:
        return  # ADMIN_ID not set — open access (warn on startup, not here)

    user = update.effective_user
    if user is None:
        return

    admin_ids: set = context.bot_data.get("admin_ids", set())
    if user.id in admin_ids:
        return

    # Block non-admin
    if update.message:
        await update.message.reply_text(
            "🚫 You don't have access to this bot.\n\n"
            "Contact the bot owner to request access."
        )
    elif update.callback_query:
        await update.callback_query.answer(
            "🚫 Access denied.", show_alert=True
        )
    raise ApplicationHandlerStop


async def post_init(application: Application):
    """Called after the application is initialized."""
    await db.init_db()
    await forwarder.load_rules_on_startup(application.bot_data)
    await userbot_bridge.init_userbot(application)

    # Build the full admin_ids set: owner + DB admins
    db_admin_ids = await db.load_admin_ids()
    admin_ids: set = db_admin_ids.copy()
    if config.OWNER_ID != 0:
        admin_ids.add(config.OWNER_ID)
    else:
        logger.warning(
            "ADMIN_ID is not set — bot is open to everyone! "
            "Set ADMIN_ID in Railway env vars to restrict access."
        )
    application.bot_data["admin_ids"] = admin_ids
    logger.info("Admin gate loaded: %d admin(s)", len(admin_ids))

    # Schedule auto-resume check
    asyncio.create_task(copybot_handler.schedule_auto_resume(application))


def build_app(token: str) -> Application:
    _data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(_data_dir, exist_ok=True)
    persistence = PicklePersistence(
        filepath=os.path.join(_data_dir, "persistence.pkl"),
        store_data=PersistenceInput(bot_data=False, chat_data=False, user_data=True),
        update_interval=1,
    )

    app = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    # ── Admin gate — runs before everything else ──────────────────────────────
    app.add_handler(TypeHandler(Update, _admin_gate), group=-1)

    # ── Admin management conversation ─────────────────────────────────────────
    admin_conv = admin_mgmt_handler.build_admin_conv()

    # ── Main menu conversation handler ───────────────────────────────────────
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", menu_handler.start),
            CommandHandler("menu", menu_handler.start),
        ],
        allow_reentry=True,
        states={
            MAIN_MENU: [
                CallbackQueryHandler(rules_handler.add_rule_start,        pattern="^add_rule$"),
                CallbackQueryHandler(rules_handler.list_rules,             pattern="^list_rules$"),
                CallbackQueryHandler(rules_handler.delete_rule_start,      pattern="^delete_rule$"),
                CallbackQueryHandler(history_handler.history_start,        pattern="^fwd_history$"),
                CallbackQueryHandler(ignore_handler.ignore_list_menu,      pattern="^ignore_list$"),
                CallbackQueryHandler(ignore_handler.ignore_add_start,      pattern="^ignore_add$"),
                CallbackQueryHandler(ignore_handler.ignore_remove_start,   pattern="^ignore_remove$"),
                CallbackQueryHandler(menu_handler.menu,                    pattern="^menu$"),
                CallbackQueryHandler(menu_handler.help_cmd,                pattern="^help$"),
                CallbackQueryHandler(copybot_handler.status_callback,      pattern="^status_menu$"),
                CallbackQueryHandler(copybot_handler.listchats_callback,   pattern="^listchats_menu$"),
                CallbackQueryHandler(ignore_handler.ignore_remove_select,  pattern=r"^rm_ignore_\d+$"),
                CallbackQueryHandler(rules_handler.delete_rule_select,     pattern=r"^del_rule_\d+$"),
            ],
            ADD_RULE_SOURCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rules_handler.add_rule_source),
            ],
            ADD_RULE_DEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rules_handler.add_rule_dest),
            ],
            ADD_RULE_CONFIRM: [
                CallbackQueryHandler(rules_handler.add_rule_confirm, pattern="^rule_confirm$"),
                CallbackQueryHandler(menu_handler.menu,              pattern="^menu$"),
            ],
            DELETE_RULE_SELECT: [
                CallbackQueryHandler(rules_handler.delete_rule_select, pattern=r"^del_rule_\d+$"),
                CallbackQueryHandler(menu_handler.menu,                pattern="^menu$"),
            ],
            IGNORE_ADD_CHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ignore_handler.ignore_add_chat),
            ],
            IGNORE_REMOVE_SELECT: [
                CallbackQueryHandler(ignore_handler.ignore_remove_select, pattern=r"^rm_ignore_\d+$"),
                CallbackQueryHandler(ignore_handler.ignore_list_menu,     pattern="^ignore_list$"),
            ],
            FORWARD_HISTORY_SOURCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, history_handler.history_source),
            ],
            FORWARD_HISTORY_DEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, history_handler.history_dest),
            ],
            FORWARD_HISTORY_LIMIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, history_handler.history_limit),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", rules_handler.cancel),
            CommandHandler("start",  menu_handler.start),
        ],
        per_chat=False,
        per_user=True,
        per_message=False,
    )

    copy_conv      = copybot_handler.build_copy_conv()
    login_conv     = login_handler.build_login_conv()
    preview_conv   = preview_handler.build_preview_conv()
    gensession_conv = gensession_handler.build_gensession_conv()

    app.add_handler(preview_conv)
    app.add_handler(copy_conv)
    app.add_handler(login_conv)
    app.add_handler(gensession_conv)
    app.add_handler(admin_conv)
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", menu_handler.help_cmd))

    for h in copybot_handler.get_extra_handlers():
        app.add_handler(h)

    for h in deletesession_handler.get_deletesession_handlers():
        app.add_handler(h)

    app.add_handler(MessageHandler(filters.COMMAND, menu_handler.unknown_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler.unknown_text)
    )

    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, forwarder.handle_forward),
        group=1,
    )

    async def error_handler(update, context):
        logger.error("Unhandled exception", exc_info=context.error)

    app.add_error_handler(error_handler)

    return app
