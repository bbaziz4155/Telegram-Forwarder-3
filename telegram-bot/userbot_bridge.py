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

logger = logging.getLogger(__name__)

API_ID        = int(os.environ.get("TELEGRAM_API_ID",   "0"))
API_HASH      = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
SESSION_PATH  = os.path.join(os.path.dirname(__file__), "sessions", "userbot")

_FAST_RETRIES = 12
_FAST_DELAY   = 5
_SLOW_DELAY   = 30


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

    attempt = 0
    while True:
        attempt += 1
        client = None
        try:
            # Validate SESSION_STRING before using it.  An invalid/malformed
            # string raises an exception in StringSession(); we catch it here
            # and fall back to the file session so /login can still work.
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

            # ── Set the client BEFORE connect ──────────────────────────────
            # This is the critical fix: if connect() raises (bad session,
            # network error) bot_data["userbot_client"] is already set, so
            # login_start() won't show "still initialising" forever.
            # login_phone() handles the "disconnected" case by reconnecting.
            bot_data["userbot_client"] = client
            bot_data.pop("userbot_locked", None)

            await client.connect()

            if not await client.is_user_authorized():
                logger.warning(
                    "Userbot session not authorised — use /login in the bot to sign in."
                )
                bot_data["userbot_ready"] = False

                # Poll every 10 s until the user completes the in-bot login.
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
                    bot_data["userbot_ready"] = False
                    await asyncio.sleep(_FAST_DELAY)
                    continue

            me = await client.get_me()
            bot_data["userbot_client"] = client
            bot_data["userbot_ready"]  = True
            logger.info(f"Userbot bridge connected as {me.first_name} (@{me.username})")

            while True:
                await asyncio.sleep(30)
                try:
                    if not client.is_connected():
                        logger.warning("Userbot connection lost — reconnecting…")
                        break
                    if not await client.is_user_authorized():
                        logger.warning("Userbot session deauthorised — reconnecting…")
                        break
                except Exception as e:
                    logger.warning(f"Userbot health-check failed: {e} — reconnecting…")
                    break

            bot_data["userbot_ready"] = False
            try:
                await client.disconnect()
            except Exception:
                pass
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
                await asyncio.sleep(delay)
            else:
                logger.error(f"Userbot connect failed: {e}")
                await asyncio.sleep(_SLOW_DELAY)


async def init_userbot(application) -> None:
    """PTB post_init hook — starts the connection task and returns immediately."""
    bot_data = application.bot_data
    bot_data.setdefault("active_copy_task",    None)
    bot_data.setdefault("active_sync_task",    None)
    bot_data.setdefault("active_sync_handler", None)
    bot_data.setdefault("active_copy_stats",   {})
    bot_data.setdefault("userbot_ready",       False)

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


def is_locked(bot_data: dict) -> bool:
    return bot_data.get("userbot_locked", False)