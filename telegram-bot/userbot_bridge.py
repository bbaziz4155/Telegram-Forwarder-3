"""
Manages a shared Telethon userbot client inside PTB's asyncio event loop.

The connection is made in a background task so the bot starts polling
immediately. The client is stored in bot_data as soon as it connects
(even before authorisation) so the in-bot login wizard can use it
straight away.
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

API_ID   = int(os.environ.get("TELEGRAM_API_ID",   "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_PATH = os.path.join(os.path.dirname(__file__), "sessions", "userbot")

_FAST_RETRIES = 12
_FAST_DELAY   = 5
_SLOW_DELAY   = 30


async def _connect_loop(bot_data: dict) -> None:
    """
    Background task — connects the Telethon client and keeps it available.

    Key guarantee: bot_data["userbot_client"] is set to the connected client
    immediately after connect() succeeds, BEFORE the authorisation check.
    This means the in-bot /login wizard can always call send_code_request()
    even if the session file has no saved credentials yet.
    """
    try:
        from telethon import TelegramClient
    except ImportError:
        logger.warning("telethon not installed — userbot commands disabled")
        return

    os.makedirs(os.path.join(os.path.dirname(__file__), "sessions"), exist_ok=True)

    attempt = 0
    while True:
        attempt += 1
        client = None
        try:
            client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
            await client.connect()

            # ── Set the client IMMEDIATELY after connect, before auth check ──
            # This is critical: the login wizard checks get_client() and will
            # fail with "still initialising" if we delay setting this.
            bot_data["userbot_client"] = client
            bot_data.pop("userbot_locked", None)

            if not await client.is_user_authorized():
                logger.warning(
                    "Userbot session not authorised — use /login in the bot to sign in."
                )
                bot_data["userbot_ready"] = False

                # Poll every 10 s until the user completes the in-bot login.
                # authorised=True  → break and fall through to get_me() below.
                # Exception        → break and reconnect (connection dropped).
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
                    # Connection dropped — reconnect from scratch.
                    # Don't reset userbot_client to None — the login wizard
                    # needs the reference to call send_code_request().
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

            # Stay in the loop and monitor the connection — do NOT return.
            # If Telethon loses its connection or the session is revoked,
            # reset userbot_ready and reconnect so /copy etc. don't silently fail.
            # NOTE: is_connected() is a plain bool method (not a coroutine) in Telethon.
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

            # Connection dropped — reset state and fall through to retry loop
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
                # Don't give up — wait and retry so a transient network
                # hiccup doesn't permanently disable userbot features.
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

    # Store the task so it isn't garbage-collected before it runs.
    task = asyncio.create_task(_connect_loop(bot_data))
    bot_data["_userbot_connect_task"] = task
    logger.info("Userbot bridge task started in background")


def get_client(bot_data: dict):
    return bot_data.get("userbot_client")


def is_ready(bot_data: dict) -> bool:
    return bot_data.get("userbot_ready", False)


def is_locked(bot_data: dict) -> bool:
    return bot_data.get("userbot_locked", False)
