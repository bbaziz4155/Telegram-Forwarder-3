"""
USER CONFIGURATION — values are read from environment variables first,
then fall back to the hardcoded defaults below.

Set these in your Railway environment variables to override without
touching this file:
  SOURCE_CHANNEL, DEST_CHANNEL, CAPTION_REPLACE, NOTIFY_EVERY,
  ALLOWED_EXTS, SKIP_TEXT

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

# ── Source channel (where files are copied FROM) ─────────────────────────────
SOURCE_CHANNEL = _int_env("SOURCE_CHANNEL", -1001811670072)

# ── Destination channel (where files are copied TO) ──────────────────────────
DEST_CHANNEL = _int_env("DEST_CHANNEL", -1003563437550)

# ── Your channel link — replaces ALL @usernames AND t.me links in captions ───
# Every @username and https://t.me/... link found in a caption will be
# swapped for this value.  Set to "" to keep original text unchanged.
CAPTION_REPLACE = os.environ.get("CAPTION_REPLACE", "@BackupChannel5211")

# ── Notify every N files copied (0 = off) ────────────────────────────────────
NOTIFY_EVERY = _int_env("NOTIFY_EVERY", 100)

# ── File filter — only copy these extensions (empty = copy everything) ───────
# Examples: {"mkv"}  |  {"mkv", "mp4"}  |  set()  (copy all files)
_ext_env = os.environ.get("ALLOWED_EXTS", "").strip()
ALLOWED_EXTS: set = {e.strip().lower() for e in _ext_env.split(",") if e.strip()} if _ext_env else set()

# ── Skip plain text-only messages (season labels / hashtags are kept) ─────────
# True  = skip ALL text messages (only copy media files)
# False = copy text posts too (SEASON 02, #TAMIL, quality labels, etc.)
SKIP_TEXT = _bool_env("SKIP_TEXT", False)

# ── Promo / watermark strip patterns ─────────────────────────────────────────
# Any caption line that contains a match for one of these regex patterns will
# be removed entirely (the whole line, not just the matched word).
# Patterns are case-insensitive.  Add your own as needed.
#
# Examples from the source channel:
#   "Latest Movies - Master Print Downloader📂(MPD)5.0"
#   "Movie Request Group - MPD Requested Movies Zone 3.0📂"
#   "🔗 CHANNEL LINK 👉 https://t.me/..."
#   "FILE ADDED BY GOUTHAM SER ❤️"
STRIP_PATTERNS: list = [
    r"master\s+print\s+download",   # Master Print Downloader (MPD) promo
    r"movie\s+request\s+group",     # Movie Request Group promo
    r"channel\s+link",              # "CHANNEL LINK 👉" lines
    r"file\s+added\s+by\s+goutham", # "FILE ADDED BY GOUTHAM SER ❤️" watermark
]
