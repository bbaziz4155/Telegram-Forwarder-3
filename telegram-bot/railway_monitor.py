"""
Railway credit monitor.

Runs a background asyncio task that checks your Railway account's credit
balance every CREDIT_CHECK_HOURS hours and sends a Telegram message to
the owner (ADMIN_ID) when the balance drops below CREDIT_ALERT_THRESHOLD.

Setup — add these in Railway Variables:
  RAILWAY_TOKEN           — API token from railway.com → Settings → Tokens
  CREDIT_ALERT_THRESHOLD  — alert when balance < this (default: 1.00)
  CREDIT_CHECK_HOURS      — how often to check (default: 12)

Manual check: /creditcheck in the bot chat.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

import config

logger = logging.getLogger(__name__)

_GQL_URL = "https://backboard.railway.app/graphql/v2"

_BALANCE_QUERY = json.dumps({
    "query": "{ me { creditBalance } }"
}).encode()


def _fetch_balance_sync() -> Optional[float]:
    """Blocking HTTP call — run via asyncio.to_thread."""
    if not config.RAILWAY_TOKEN:
        return None
    req = urllib.request.Request(
        _GQL_URL,
        data=_BALANCE_QUERY,
        headers={
            "Authorization": f"Bearer {config.RAILWAY_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            val = body["data"]["me"]["creditBalance"]
            return float(val)
    except urllib.error.HTTPError as exc:
        logger.warning("Railway API HTTP %s: %s", exc.code, exc.read()[:200])
    except urllib.error.URLError as exc:
        logger.warning("Railway API network error: %s", exc.reason)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Railway API unexpected response: %s", exc)
    return None


async def get_credit_balance() -> Optional[float]:
    """Async wrapper around the blocking HTTP call."""
    return await asyncio.to_thread(_fetch_balance_sync)


async def start_credit_monitor(app) -> None:
    """
    Background loop started in post_init.
    Sleeps CREDIT_CHECK_HOURS hours between checks.
    Sends one alert when balance first drops below threshold;
    resets after balance recovers so a top-up triggers a fresh alert
    next time it drops again.
    """
    if not config.RAILWAY_TOKEN:
        logger.info("RAILWAY_TOKEN not set — Railway credit monitor disabled.")
        return

    interval  = max(1, config.CREDIT_CHECK_HOURS) * 3600
    threshold = config.CREDIT_ALERT_THRESHOLD
    owner_id  = config.OWNER_ID
    bot       = app.bot
    alerted   = False

    logger.info(
        "Railway credit monitor started (threshold=$%.2f, interval=%dh).",
        threshold, config.CREDIT_CHECK_HOURS,
    )

    while True:
        await asyncio.sleep(interval)
        balance = await get_credit_balance()
        if balance is None:
            continue

        logger.info("Railway credit balance: $%.4f", balance)

        if balance < threshold:
            if not alerted and owner_id:
                try:
                    await bot.send_message(
                        chat_id=owner_id,
                        text=(
                            "⚠️ *Railway Credit Alert*\n\n"
                            f"Your Railway balance is *${balance:.2f}*, which is below the "
                            f"*${threshold:.2f}* alert threshold.\n\n"
                            "Top up at [railway.com/account/billing](https://railway.com/account/billing) "
                            "to keep your bot running.\n\n"
                            "_You can change the threshold with the `CREDIT_ALERT_THRESHOLD` "
                            "Railway Variable._"
                        ),
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                    alerted = True
                    logger.warning(
                        "Low-credit alert sent to owner (balance=$%.4f, threshold=$%.2f).",
                        balance, threshold,
                    )
                except Exception as exc:
                    logger.error("Failed to send credit alert: %s", exc)
        else:
            if alerted:
                logger.info("Balance recovered to $%.4f — alert reset.", balance)
            alerted = False


async def creditcheck_cmd(update, context) -> None:
    """
    /creditcheck — instantly check Railway credit balance on demand.
    """
    if not config.RAILWAY_TOKEN:
        await update.message.reply_text(
            "ℹ️ *Railway credit monitor is not configured.*\n\n"
            "To enable it:\n"
            "1. Go to *railway.com → Settings → Tokens* → create a new token\n"
            "2. Add it as `RAILWAY_TOKEN` in your Railway Variables\n"
            "3. Redeploy — the bot will then check your balance automatically\n\n"
            "Optional variables:\n"
            "• `CREDIT_ALERT_THRESHOLD` — alert when balance < this (default: $1.00)\n"
            "• `CREDIT_CHECK_HOURS` — check interval in hours (default: 12)",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("🔄 Checking Railway balance…")
    balance = await get_credit_balance()

    if balance is None:
        await update.message.reply_text(
            "❌ *Could not fetch Railway balance.*\n\n"
            "Possible reasons:\n"
            "• `RAILWAY_TOKEN` is invalid or expired\n"
            "• Railway API is temporarily unreachable\n\n"
            "Check your token at *railway.com → Settings → Tokens*.",
            parse_mode="Markdown",
        )
        return

    threshold = config.CREDIT_ALERT_THRESHOLD
    if balance >= threshold:
        status = f"✅ Balance is healthy (above ${threshold:.2f} threshold)"
        emoji  = "💚"
    elif balance > 0:
        status = f"⚠️ Balance is *below the ${threshold:.2f} alert threshold!*"
        emoji  = "🟡"
    else:
        status = "🚨 *Balance is $0.00 — bot may be shut down soon!*"
        emoji  = "🔴"

    await update.message.reply_text(
        f"{emoji} *Railway Credit Balance*\n\n"
        f"💰 Available: *${balance:.4f}*\n"
        f"🎯 Alert threshold: ${threshold:.2f}\n"
        f"⏱ Auto-check every: {config.CREDIT_CHECK_HOURS}h\n\n"
        f"{status}\n\n"
        "_Top up at [railway.com/account/billing](https://railway.com/account/billing)_",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
