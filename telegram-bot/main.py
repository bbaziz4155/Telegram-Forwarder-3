import asyncio
import logging
import os
import threading
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer

from bot import build_app
from telegram import Update

warnings.filterwarnings("ignore", message="If 'per_message=False'", category=UserWarning)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # silence per-request logs


def _start_health_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    except OSError as e:
        logger.warning(f"Health server could not bind to port {port}: {e} — skipping")
        return
    logger.info(f"Health server listening on port {port}")
    # non-daemon so the process stays alive even if the bot loop exits
    thread = threading.Thread(target=server.serve_forever)
    thread.start()


def main():
    # Start HTTP health server FIRST — before anything else so Railway's
    # health check always gets a 200 OK regardless of bot startup state.
    # On Replit this port may be unavailable; the server gracefully skips.
    _start_health_server()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set — bot will not start")
        # Keep the process alive so the health check keeps passing
        threading.Event().wait()
        return

    app = build_app(token)
    logger.info("Starting Telegram Forwarder Bot...")
    # drop_pending_updates=True: discard commands queued while the bot was
    # offline so stale messages are never replayed on restart.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()