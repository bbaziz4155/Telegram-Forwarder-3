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
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info(f"Health server listening on port {port}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

    # Start HTTP health server in background thread FIRST — completely
    # independent of the bot's asyncio loop so Railway's health check passes
    # even while the bot is still initialising.
    _start_health_server()

    app = build_app(token)
    logger.info("Starting Telegram Forwarder Bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
