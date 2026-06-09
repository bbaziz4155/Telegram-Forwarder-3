import asyncio
import os
import logging
import warnings
from aiohttp import web
from bot import build_app
from telegram import Update

warnings.filterwarnings("ignore", message="If 'per_message=False'", category=UserWarning)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def health_handler(request):
    return web.Response(text="OK")


async def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/healthz", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server listening on port {port}")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    tg_app = build_app(token)
    logger.info("Starting Telegram Forwarder Bot...")

    async def _run():
        await run_health_server()
        async with tg_app:
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            # Run forever until interrupted
            await asyncio.Event().wait()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
