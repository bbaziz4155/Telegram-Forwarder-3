import os
import logging
from bot import build_app
from telegram import Update

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    app = build_app(token)
    logger.info("Starting Telegram Forwarder Bot...")
    # run_polling manages its own event loop — do NOT wrap in asyncio.run()
    # drop_pending_updates=True: ignore messages queued while the bot was offline.
    # Without this, restarting the bot could trigger forward rules on old messages.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
