"""
USER CONFIGURATION — edit this file once, everything else works automatically.

Set your channel IDs and username here so you never have to type them
again in the bot or terminal.

How to find a channel ID:
  Forward any message from the channel to @userinfobot — it shows the ID.
  Private channels always start with -100...
"""

# ── Source channel (where files are copied FROM) ─────────────────────────────
# Example: -1001811670072  (Kuttu Bot™ FiLes)
SOURCE_CHANNEL = -1001811670072

# ── Destination channel (where files are copied TO) ──────────────────────────
# Example: -1003563437550  (Pvt movie channel)
DEST_CHANNEL = -1003563437550

# ── Your channel link — replaces ALL @usernames AND t.me links in captions ───
# Every @username and https://t.me/... link found in a caption will be
# swapped for this value.  Set to "" to keep original text unchanged.
CAPTION_REPLACE = "@BackupChannel5211"

# ── Notify every N files copied (0 = off) ────────────────────────────────────
NOTIFY_EVERY = 100

# ── File filter — only copy these extensions (empty = copy everything) ───────
# Examples: {"mkv"}  |  {"mkv", "mp4"}  |  set()  (copy all files)
ALLOWED_EXTS: set = set()

# ── Skip plain text-only messages (season labels / hashtags are kept) ─────────
# True  = skip ALL text messages (only copy media files)
# False = copy text posts too (SEASON 02, #TAMIL, quality labels, etc.)
SKIP_TEXT = False

# ── Promo / watermark strip patterns ─────────────────────────────────────────
# Any caption line that contains a match for one of these regex patterns will
# be removed entirely (the whole line, not just the matched word).
# Patterns are case-insensitive.  Add your own as needed.
#
# Examples from the source channel:
#   "Latest Movies - Master Print Downloader📂(MPD)5.0"
#   "Movie Request Group - MPD Requested Movies Zone 3.0📂"
#   "🔗 CHANNEL LINK 👉 https://t.me/..."
STRIP_PATTERNS: list = [
    r"master\s+print\s+download",   # Master Print Downloader (MPD) promo
    r"movie\s+request\s+group",     # Movie Request Group promo
    r"channel\s+link",              # "CHANNEL LINK 👉" lines
]
