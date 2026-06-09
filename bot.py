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
    filters,
)
import database as db
import forwarder
import userbot_bridge
from handlers import menu as menu_handler
from handlers import rules as rules_handler
from handlers import history as history_handler
from handlers import ignore as ignore_handler
from handlers import copybot as copybot_handler
from handlers import login as login_handler
from handlers import preview as preview_handler
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


async def _health_server_task():
    """
    Tiny HTTP server that responds 200 OK to any request.

    Keeps Render / Railway / Koyeb Web Service probes happy so the container
    is never marked unhealthy and never spun down for inactivity.
    Binds to $PORT (set automatically by Render/Railway) or 8080 as fallback.
    Safe to run on Replit too — it just opens an unused port and idles there.
    """
    port = int(os.environ.get("PORT", "8080"))

    async def _handle(reader, writer):
        try:
            await reader.read(4096)          # drain the HTTP request
        except Exception:
            pass
        body = b"OK"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )
        try:
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(_handle, "0.0.0.0", port)
        logger.info("Health-check server listening on port %d", port)
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.warning("Health-check server failed to start on port %d: %s", port, e)


async def post_init(application: Application):
    """Called after the application is initialized."""
    await db.init_db()
    await forwarder.load_rules_on_startup(application.bot_data)
    await userbot_bridge.init_userbot(application)
    # Schedule auto-resume check — runs in the background after userbot connects
    asyncio.create_task(copybot_handler.schedule_auto_resume(application))
    # Start the keep-alive health server (keeps Render/Railway alive, harmless on Replit)
    asyncio.create_task(_health_server_task())


def build_app(token: str) -> Application:
    # Persist conversation states (user_data) to disk so login flows and rule
    # wizards survive bot restarts.  bot_data is excluded because it holds the
    # unpicklable Telethon TelegramClient object.
    _data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(_data_dir, exist_ok=True)
    persistence = PicklePersistence(
        filepath=os.path.join(_data_dir, "persistence.pkl"),
        store_data=PersistenceInput(bot_data=False, chat_data=False, user_data=True),
        update_interval=1,   # save every 1 s so a bot restart never loses login state
    )

    app = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

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
                # NOTE: "userbot_login" is NOT handled here — it falls through
                # to login_conv's entry_point so the login conversation states work.
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

    # ── Copy / dryrun / sync conversation handler ────────────────────────────
    copy_conv = copybot_handler.build_copy_conv()

    login_conv = login_handler.build_login_conv()

    preview_conv = preview_handler.build_preview_conv()

    # preview_conv is first: when in PREVIEW_AWAIT_MSG state any non-command
    # message goes there before copy_conv or conv can intercept it.
    # copy_conv and login_conv are registered BEFORE conv so that /copy,
    # /dryrun, /sync, and /login always work even when the user is stuck
    # in a stale conv state (e.g. Forward History awaiting input).
    app.add_handler(preview_conv)
    app.add_handler(copy_conv)
    app.add_handler(login_conv)
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", menu_handler.help_cmd))

    # Standalone userbot control commands
    for h in copybot_handler.get_extra_handlers():
        app.add_handler(h)

    # Fallback: unknown /commands and plain text outside any conversation
    app.add_handler(MessageHandler(filters.COMMAND, menu_handler.unknown_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler.unknown_text)
    )

    # Live forwarder — listens to ALL messages in ALL chats (group 1 runs after conv)
    # filters.ALL already covers UpdateType.CHANNEL_POSTS so we only need one handler.
    # Two handlers for the same function would double-forward every channel post.
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, forwarder.handle_forward),
        group=1,
    )

    # Error handler — log all handler exceptions so nothing is silently swallowed
    async def error_handler(update, context):
        logger.error("Unhandled exception", exc_info=context.error)

    app.add_error_handler(error_handler)

    return app
