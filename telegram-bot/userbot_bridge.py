"""
Manages a shared Telethon userbot client inside PTB's asyncio event loop.

The connection is made in a background task so the bot starts polling
immediately. The client is stored in bot_data as soon as it is created
(BEFORE connect()) so the in-bot login wizard can always find it — even
if the initial connection fails due to a bad SESSION_STRING, network
hiccup, or other transient error.
"""
import asyncio
import logging
import os

import config  # for OWNER_ID in revocation alerts

logger = logging.getLogger(__name__)

API_ID        = int(os.environ.get("TELEGRAM_API_ID",   "0"))
API_HASH      = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
SESSION_PATH  = os.path.join(os.path.dirname(__file__), "sessions", "userbot")

_FAST_RETRIES = 12
_FAST_DELAY   = 5
_SLOW_DELAY   = 30


async def _send_revocation_alert(bot_data: dict, during_copy: bool) -> None:
    """
    Send a Telegram alert to the owner (and the copy chat if different)
    when Telegram revokes the userbot session.
    """
    ptb_bot  = bot_data.get("_ptb_bot")
    owner_id = bot_data.get("_owner_id", 0)

    if not ptb_bot or not owner_id:
        logger.warning("Session revoked but cannot alert — _ptb_bot or _owner_id not set.")
        return

    # Prefer notifying whichever chat started the copy job, fallback to owner
    notify_chat = bot_data.get("active_copy_chat_id") or owner_id

    if during_copy:
        text = (
            "⚠️ *Userbot Session Revoked Mid\\-Copy\\!*\n\n"
            "Telegram kicked your session and the copy job was automatically stopped\\.\n\n"
            "*To resume:*\n"
            "1\\. Run /gensession to generate a new session string\n"
            "2\\. Set `SESSION_STRING` in Railway with the new value\n"
            "3\\. Wait for Railway to redeploy \\(\\~30 seconds\\)\n"
            "4\\. Run /resume to pick up exactly where you left off\n\n"
            "_Tip: Running at 🛡 Safe speed reduces how often Telegram revokes sessions\\._"
        )
    else:
        text = (
            "⚠️ *Userbot Session Revoked*\n\n"
            "Telegram invalidated your session\\. The userbot is now disconnected\\.\n\n"
            "Run /gensession to reconnect\\."
        )

    chats_notified = set()
    for chat_id in {notify_chat, owner_id}:
        if chat_id and chat_id not in chats_notified:
            try:
                await ptb_bot.send_message(chat_id, text, parse_mode="MarkdownV2")
                chats_notified.add(chat_id)
            except Exception as e:
                logger.error("Revocation alert to %s failed: %s", chat_id, e)


async def _connect_loop(bot_data: dict) -> None:
    """
    Background task — connects the Telethon client and keeps it available.

    Key guarantee: bot_data["userbot_client"] is set to the client object
    BEFORE connect() is awaited.  This means the in-bot /login wizard always
    finds a usable client even if the initial connection attempt fails (bad
    SESSION_STRING, network error, etc.) — it just reconnects inside
    login_phone() before calling send_code_request().
    """
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        logger.warning("telethon not installed — userbot commands disabled")
        return

    os.makedirs(os.path.join(os.path.dirname(__file__), "sessions"), exist_ok=True)

    def _is_auth_error(exc: Exception) -> bool:
        """Return True for Telegram auth-key / unauthorised errors."""
        name = type(exc).__name__
        msg  = str(exc).lower()
        return (
            "authkey" in name.lower()
            or "unauthorized" in name.lower()
            or "authorization key" in msg
            or "auth_key" in msg
        )

    def _cancel_copy_task(bot_data: dict) -> bool:
        """Cancel any running copy task. Returns True if one was cancelled."""
        task = bot_data.get("active_copy_task")
        if task and not task.done():
            task.cancel()
            bot_data["active_copy_task"] = None
            return True
        return False

    attempt = 0
    while True:
        attempt += 1
        client = None
        try:
            if SESSION_STRING:
                try:
                    session = StringSession(SESSION_STRING)
                    logger.info("Userbot: using StringSession from env")
                except Exception as e:
                    logger.warning(
                        "SESSION_STRING is invalid (%s) — "
                        "falling back to file session so /login can work.", e
                    )
                    session = SESSION_PATH
            else:
                session = SESSION_PATH
                logger.info("Userbot: using file session at %s", SESSION_PATH)

            client = TelegramClient(session, API_ID, API_HASH)

            bot_data["userbot_client"] = client
            bot_data["userbot_reason"] = "connecting"
            bot_data.pop("userbot_locked", None)

            # Auto-sleep flood waits up to 60 s so short waits (3-60 s) are
            # absorbed silently without crashing the copy loop.  Waits > 60 s
            # are raised as FloodWaitError and handled explicitly by the copy
            # engine (with progress notification and checkpoint save).
            # Setting this to 0 was a bug: it caused every flood wait —
            # including the routine 3-second GetHistoryRequest wait — to raise
            # an exception that killed the copy job.
            client.flood_sleep_threshold = 60

            await client.connect()

            if not await client.is_user_authorized():
                logger.warning(
                    "Userbot session not authorised — use /login in the bot to sign in."
                )
                bot_data["userbot_ready"]  = False
                bot_data["userbot_reason"] = "needs_login"

                authorised = False
                while True:
                    await asyncio.sleep(10)
                    try:
                        if await client.is_user_authorized():
                            authorised = True
                            break
                    except Exception:
                        logger.warning("Userbot auth-poll error — reconnecting")
                        break

                if not authorised:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    bot_data["userbot_ready"]  = False
                    bot_data["userbot_reason"] = "needs_login"
                    await asyncio.sleep(_FAST_DELAY)
                    continue

            me = await client.get_me()
            bot_data["userbot_client"] = client
            bot_data["userbot_ready"]  = True
            bot_data["userbot_reason"] = ""
            bot_data.pop("_revocation_alerted", None)  # clear so next revocation sends a fresh alert
            logger.info(f"Userbot bridge connected as {me.first_name} (@{me.username})")

            # ── Health-check loop ──────────────────────────────────────────
            while True:
                await asyncio.sleep(30)
                try:
                    if not client.is_connected():
                        logger.warning("Userbot connection lost — reconnecting…")
                        bot_data["userbot_reason"] = "reconnecting"
                        break
                    if not await client.is_user_authorized():
                        logger.warning("Userbot session deauthorised — sending alert…")
                        bot_data["userbot_reason"] = "session_revoked"
                        copy_was_running = _cancel_copy_task(bot_data)
                        if copy_was_running:
                            bot_data["session_lost_during_copy"] = True
                            logger.warning(
                                "Active copy task cancelled because session was deauthorised."
                            )
                        if not bot_data.get("_revocation_alerted"):
                            bot_data["_revocation_alerted"] = True
                            await _send_revocation_alert(bot_data, during_copy=copy_was_running)
                        break
                except Exception as e:
                    logger.warning(f"Userbot health-check failed: {e} — reconnecting…")
                    bot_data["userbot_reason"] = "reconnecting"
                    break

            bot_data["userbot_ready"] = False
            try:
                await client.disconnect()
            except Exception:
                pass
            # Session revoked → don't spam reconnects with the same dead string.
            # Railway will restart the container when user sets a new SESSION_STRING.
            if bot_data.get("userbot_reason") == "session_revoked":
                logger.warning(
                    "Session revoked — waiting for redeploy with new SESSION_STRING. "
                    "Run /gensession, update Railway env var, then let it redeploy."
                )
                while bot_data.get("userbot_reason") == "session_revoked":
                    await asyncio.sleep(60)
                continue
            await asyncio.sleep(_FAST_DELAY)
            continue

        except Exception as e:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            if "database is locked" in str(e).lower():
                if attempt == 1:
                    logger.warning(
                        "Userbot session locked — will retry automatically "
                        "(every 5 s for 1 min, then every 30 s)."
                    )
                    bot_data["userbot_locked"] = True
                elif attempt == _FAST_RETRIES + 1:
                    logger.warning("Session still locked — switching to slow retries (30 s).")
                delay = _FAST_DELAY if attempt <= _FAST_RETRIES else _SLOW_DELAY
                bot_data["userbot_reason"] = "reconnecting"
                await asyncio.sleep(delay)
            elif _is_auth_error(e):
                logger.error(f"Userbot auth error (session invalid/revoked): {e}")
                bot_data["userbot_reason"] = "session_revoked"
                copy_was_running = _cancel_copy_task(bot_data)
                if copy_was_running:
                    bot_data["session_lost_during_copy"] = True
                    logger.warning("Active copy task cancelled due to auth error.")
                if not bot_data.get("_revocation_alerted"):
                    bot_data["_revocation_alerted"] = True
                    await _send_revocation_alert(bot_data, during_copy=copy_was_running)
                logger.warning("Session revoked — waiting for redeploy with new SESSION_STRING.")
                while bot_data.get("userbot_reason") == "session_revoked":
                    await asyncio.sleep(60)
            else:
                logger.error(f"Userbot connect failed: {e}")
                bot_data["userbot_reason"] = "reconnecting"
                await asyncio.sleep(_SLOW_DELAY)


async def init_userbot(application) -> None:
    """PTB post_init hook — starts the connection task and returns immediately."""
    bot_data = application.bot_data
    bot_data.setdefault("active_copy_task",    None)
    bot_data.setdefault("active_sync_task",    None)
    bot_data.setdefault("active_sync_handler", None)
    bot_data.setdefault("active_copy_stats",   {})
    bot_data.setdefault("userbot_ready",       False)

    # Store PTB bot + owner ID so the background connect loop can send alerts
    bot_data["_ptb_bot"]  = application.bot
    bot_data["_owner_id"] = config.OWNER_ID

    if not API_ID or not API_HASH:
        logger.warning("TELEGRAM_API_ID/HASH not set — userbot commands disabled")
        return

    task = asyncio.create_task(_connect_loop(bot_data))
    bot_data["_userbot_connect_task"] = task
    logger.info("Userbot bridge task started in background")


def get_client(bot_data: dict):
    return bot_data.get("userbot_client")


def is_ready(bot_data: dict) -> bool:
    return bot_data.get("userbot_ready", False)


def is_starting_up(bot_data: dict) -> bool:
    """True while the bridge is actively connecting/reconnecting."""
    return bot_data.get("userbot_reason", "") in ("connecting", "reconnecting")


def is_locked(bot_data: dict) -> bool:
    return bot_data.get("userbot_locked", False)
