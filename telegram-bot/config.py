"""
USER CONFIGURATION — values are read from environment variables first,
then fall back to the hardcoded defaults below.

Set these in your Railway environment variables to override without
touching this file:
  ADMIN_ID, SOURCE_CHANNEL, DEST_CHANNEL, CAPTION_REPLACE,
  CAPTION_SUFFIX, NOTIFY_EVERY, ALLOWED_EXTS, SKIP_TEXT,
  RAILWAY_TOKEN, CREDIT_ALERT_THRESHOLD, CREDIT_CHECK_HOURS

How to find a channel ID:
  Forward any message from the channel to @userinfobot — it shows the ID.
  Private channels always start with -100...
"""
import os

def _int_env(key: str, default: int) -> int:
    val = os.environ.get(key, "").strip()
    try:
        return int(val) if val else default
    except ValueError:
        return default

def _bool_env(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes")

# ── Owner / admin ─────────────────────────────────────────────────────────────
# Set ADMIN_ID in Railway to your Telegram user ID.
# This account always has full access and cannot be removed via the bot.
# If not set (0), the bot is open to everyone — set it before going live!
OWNER_ID = _int_env("ADMIN_ID", 0)

# ── Source channel (where files are copied FROM) ─────────────────────────────
# Set SOURCE_CHANNEL in Railway Variables.  Default is 0 (not set) — the bot
# will refuse to start a job until a channel is configured via /setsource or
# the env var.  Do NOT hardcode a real channel ID here; use /setsource instead
# so the value is stored in channel_settings.json and survives redeploys.
SOURCE_CHANNEL = _int_env("SOURCE_CHANNEL", 0)

# ── Destination channel (where files are copied TO) ──────────────────────────
# Set DEST_CHANNEL in Railway Variables.  Default is 0 (not set).
# Use /setdest in the bot to configure and persist the value.
DEST_CHANNEL = _int_env("DEST_CHANNEL", 0)

# ── Your channel link — replaces ALL @usernames AND t.me links in captions ───
CAPTION_REPLACE = os.environ.get("CAPTION_REPLACE", "")

# ── Caption suffix — appended as a new line to every copied file's caption ───
# Set CAPTION_SUFFIX in Railway Variables to pre-configure the watermark so
# you don't have to run /setcaption after every fresh deployment.
# Example: CAPTION_SUFFIX=📌 @YourChannel
# Leave empty (the default) to copy captions as-is.
# /setcaption in the bot overrides this per-session; clearing it with
# /setcaption off falls back to this env-var default on the next restart.
CAPTION_SUFFIX = os.environ.get("CAPTION_SUFFIX", "")

# ── Notify every N files copied (0 = off) ────────────────────────────────────
NOTIFY_EVERY = _int_env("NOTIFY_EVERY", 100)

# ── File filter — only copy these extensions (empty = copy everything) ───────
_ext_env = os.environ.get("ALLOWED_EXTS", "").strip()
ALLOWED_EXTS: set = {e.strip().lower() for e in _ext_env.split(",") if e.strip()} if _ext_env else set()

# ── Skip plain text-only messages (season labels / hashtags are kept) ─────────
SKIP_TEXT = _bool_env("SKIP_TEXT", False)


# ── Promo / watermark strip patterns ─────────────────────────────────────────
STRIP_PATTERNS: list = [
    r"master\s+print\s+download",
    r"movie\s+request\s+group",
    r"channel\s+link",
    r"file\s+added\s+by\s+goutham",
]
