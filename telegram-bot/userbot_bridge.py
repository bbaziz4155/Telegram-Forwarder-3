"""
Manages a shared Telethon userbot client inside PTB's asyncio event loop.

The connection is made in a background task so the bot starts polling
immediately. The client is stored in bot_data as soon as it is created
(BEFORE connect()) so the in-bot login wizard can always find it — even
if the initial connection fails due to a bad SESSION_STRING, network
hiccup, or other transient error.

Session-revocation handling
---------------------------
When Telegram revokes the session, we write a small flag file
(DATA_DIR/session_revoked.flag) containing a hash of the current
SESSION_STRING.  On every subsequent startup, we check that file first:
- If it matches the current SESSION_STRING → we know the string is dead.
  We skip the connection attempt entirely, set the "needs_gensession" state,
  and send ONE alert (not one per restart).
- If the SESSION_STRING has changed (user ran /gensession and updated
  Railway) → we delete the flag and connect normally.
This stops the spam of "Session Revoked" alerts that appeared whenever
Railway restarted the container with the same dead session string.
"""
import asyncio
import hashlib
import json
import logging
import os

import config  # for OWNER_ID in revocation alerts

logger = logging.getLogger(__name__)

API_ID         = int(os.environ.get("TELEGRAM_API_ID",   "0"))
API_HASH       = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
SESSION_PATH   = os.path.join(os.path.dirname(__file__), "sessions", "userbot")

_DATA_DIR    = os.environ.get("DATA_DIR",
               os.path.join(os.path.dirname(__file__), "data"))
_REVOKED_FLAG = os.path.join(_DATA_DIR, "session_revoked.flag")

_FAST_RETRIES = 12
_FAST_DELAY   = 5
_SLOW_DELAY   = 30


def _session_hash() -> str:
    """Stable hash of the current SESSION_STRING (or session file path)."""
    key = SESSION_STRING.strip() if SESSION_STRING else SESSION_PATH
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _write_revoked_flag() -> None:
    """Persist the hash of the dead session so future restarts skip reconnect."""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_REVOKED_FLAG, "w") as f:
            json.dump({"session_hash": _session_hash()}, f)
        logger.info("Session revoked flag written (%s).", _REVOKED_FLAG)
    except Exception as e:
        logger.warning("Could not write session revoked flag: %s", e)


def _clear_revoked_flag() -> None:
    """Remove the revocation flag (called after successful auth with new session)."""
    try:
        os.remove(_REVOKED_FLAG)
        logger.info("Session revoked flag cleared.")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not clear session revoked flag: %s", e)


def _session_is_known_revoked() -> bool:
    """
    Return True if the current SESSION_STRING matches the hash stored in
    the revocation flag file — meaning Telegram already rejected this
    exact session string and we should not attempt to connect again.
    """
    try:
        with open(_REVOKED_FLAG) as f:
            data = json.load(f)
        return data.get("session_hash") == _session_hash()
    except FileNotFoundError:
        return False
    except Exception:
        return False


async def _send_revocation_alert(bot_data: dict, during_copy: bool,
                                  is_repeat: bool = False) -> None:
    """
    Send a Telegram alert to the owner when Telegram revokes the userbot session.
    is_repeat=True → send a shorter "still waiting" note instead of the full alert.
    """
    ptb_bot  = bot_data.get("_ptb_bot")
    owner_id = bot_data.get("_owner_id", 0)

    if not ptb_bot or not owner_id:
        logger.warning("Session revoked but cannot alert — _ptb_bot or _owner_id not set.")
        return

    notify_chat = bot_data.get("active_copy_chat_id") or owner_id

    if is_repeat:
        text = (
            "ℹ️ *Userbot still disconnected*\n\n"
            "The session string is already revoked\\. "
            "Run /gensession, update `SESSION_STRING` in Railway, then redeploy\\."
        )
    elif during_copy:
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
            "*Steps to fix:*\n"
            "1\\. Run /gensession to get a fresh session string\n"
            "2\\. Update `SESSION_STRING` in your Railway environment variables\n"
            "3\\. Railway will redeploy automatically \\(\\~30 seconds\\)\n\n"
            "_You will NOT see this alert again for the same session string — "
            "no more repeated notifications on restart\\._"
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
    """
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        logger.warning("telethon not installed — userbot commands disabled")
        return

    os.makedirs(os.path.join(os.path.dirname(__file__), "sessions"), exist_ok=True)

    def _is_auth_error(exc: Exception) -> bool:
        name = type(exc).__name__
        msg  = str(exc).lower()
        return (
            "authkey" in name.lower()
            or "unauthorized" in name.lower()
            or "authorization key" in msg
            or "auth_key" in msg
        )

    def _cancel_copy_task(bot_data: dict) -> bool:
        task = bot_data.get("active_copy_task")
        if task and not task.done():
            task.cancel()
            bot_data["active_copy_task"] = None
            return True
        return False

    # ── Pre-flight: skip connection if this session string is already known dead ──
    if _session_is_known_revoked():
        logger.warning(
            "Session string matches a previously revoked session — "
            "skipping connection attempt. Update SESSION_STRING and redeploy."
        )
        bot_data["userbot_ready"]  = False
        bot_data["userbot_reason"] = "session_revoked"
        # Send a single short "still waiting" alert, then stop.
        await asyncio.sleep(5)  # let PTB fully start before sending
        await _send_revocation_alert(bot_data, during_copy=False, is_repeat=True)
        while _session_is_known_revoked():
            await asyncio.sleep(60)
        logger.info("SESSION_STRING changed — resuming normal connection loop.")
        # Fall through to the normal connect loop below

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

            # Use stable device info so Telegram trusts the session across
            # IP changes (Railway containers may get different IPs on restart).
            client = TelegramClient(
                session, API_ID, API_HASH,
                device_model="Desktop",
                system_version="Linux x86_64",
                app_version="4.16.4",
                lang_code="en",
                system_lang_code="en-US",
            )

            bot_data["userbot_client"] = client
            bot_data["userbot_reason"] = "connecting"
            bot_data.pop("userbot_locked", None)

            # Auto-sleep flood waits up to 60 s so short waits are absorbed
            # silently. Waits > 60 s raise FloodWaitError for the copy engine.
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
            # Clear the revocation flag — this session string is working fine
            _clear_revoked_flag()
            bot_data.pop("_revocation_alerted", None)
            logger.info("Userbot bridge connected as %s (@%s)", me.first_name, me.username)

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
                        _write_revoked_flag()
                        await _send_revocation_alert(bot_data, during_copy=copy_was_running)
                        break
                except Exception as e:
                    logger.warning("Userbot health-check failed: %s — reconnecting…", e)
                    bot_data["userbot_reason"] = "reconnecting"
                    break

            bot_data["userbot_ready"] = False
            try:
                await client.disconnect()
            except Exception:
                pass

            if bot_data.get("userbot_reason") == "session_revoked":
                logger.warning(
                    "Session revoked — waiting until SESSION_STRING is updated."
                )
                while _session_is_known_revoked():
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
                logger.error("Userbot auth error (session invalid/revoked): %s", e)
                bot_data["userbot_reason"] = "session_revoked"
                copy_was_running = _cancel_copy_task(bot_data)
                if copy_was_running:
                    bot_data["session_lost_during_copy"] = True
                _write_revoked_flag()
                await _send_revocation_alert(bot_data, during_copy=copy_was_running)
                logger.warning("Session revoked — waiting until SESSION_STRING is updated.")
                while _session_is_known_revoked():
                    await asyncio.sleep(60)
            else:
                logger.error("Userbot connect failed: %s", e)
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
    return bot_data.get("userbot_reason", "") in ("connecting", "reconnecting")


def is_locked(bot_data: dict) -> bool:
    return bot_data.get("userbot_locked", False)
