import logging
import os
import threading
import time
import urllib.request
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


def _start_self_pinger():
    """
    Render free-tier services sleep after ~15 min with no inbound HTTP traffic.
    This thread pings the app's own health endpoint every 10 minutes so it
    never goes to sleep — no UptimeRobot or external service needed.

    Only activates when RENDER_EXTERNAL_URL is set (Render injects this
    automatically). On Railway, Google Cloud, or local runs this does nothing.
    """
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        return  # not on Render — skip silently

    ping_url = render_url + "/"
    logger.info(f"Render self-pinger active → pinging {ping_url} every 10 min")

    def _loop():
        while True:
            time.sleep(600)  # 10 minutes
            try:
                urllib.request.urlopen(ping_url, timeout=10)
                logger.debug("Self-ping OK")
            except Exception as exc:
                logger.debug(f"Self-ping failed (non-fatal): {exc}")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def main():
    # Start HTTP health server FIRST so any platform health check gets a
    # 200 OK immediately, before the bot itself finishes starting up.
    _start_health_server()

    # Keep the Render free-tier service awake automatically — no external
    # uptime monitor needed.
    _start_self_pinger()

    # Support both BOT_TOKEN (Railway convention) and TELEGRAM_BOT_TOKEN
    token = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error(
            "Neither BOT_TOKEN nor TELEGRAM_BOT_TOKEN is set — bot will not start. "
            "Set BOT_TOKEN in your environment variables."
        )
        # Keep the process alive so the health check keeps passing
        threading.Event().wait()
        return

    # ── Startup syntax check ─────────────────────────────────────────────────
    # Catches SyntaxErrors in any handler file before the platform's health
    # check fails with a cryptic traceback.
    import ast as _ast, glob as _glob
    _bot_dir = os.path.dirname(os.path.abspath(__file__))
    _py_files = _glob.glob(os.path.join(_bot_dir, "**", "*.py"), recursive=True)
    _syntax_errors = []
    for _pf in sorted(_py_files):
        try:
            with open(_pf) as _f:
                _ast.parse(_f.read(), filename=_pf)
        except SyntaxError as _se:
            _syntax_errors.append(f"{os.path.relpath(_pf, _bot_dir)}: line {_se.lineno} — {_se.msg}")
    if _syntax_errors:
        logger.error(
            "\n\n🚨 STARTUP SYNTAX CHECK FAILED:\n%s\n\n"
            "Fix the error above and redeploy.",
            "\n".join(_syntax_errors),
        )
        threading.Event().wait()  # keep health server alive; do not start bot
        return
    logger.info("Startup syntax check passed (%d files checked).", len(_py_files))

    app = build_app(token)
    logger.info("Starting Telegram Forwarder Bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
