"""
Caption cleaning and file-type filtering utilities.

  clean_caption(text, replacement)        — swap every @username AND t.me link with replacement
  matches_filter(message, exts, skip_text) — True if message passes the filter

What "deleted text from channel" means in Telethon
---------------------------------------------------
When a media message is deleted in a channel, Telegram keeps a placeholder in
the message list. In Telethon this shows up as:
  message.media  →  MessageMediaEmpty  (NOT None, NOT a real file)
These placeholders look like empty text to a human but carry no usable content.

We ALWAYS block:
  • MessageMediaEmpty  — deleted-media placeholders
  • Service messages   — join/leave/pin/topic actions (message.action is set)
  • Completely empty   — no text AND no usable media

We let through by default (skip_text=False):
  • "SEASON 02", "SEASON 03"  — channel text posts
  • "#TAMIL #Malayalam #720p" — hashtag posts
  • Quality / info labels

skip_text=True is an extra opt-in that also blocks those text-only posts.
"""
import os
import re

# Matches @username (3–32 chars, letters/digits/underscore)
_USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{3,32}")

# Matches all Telegram channel/invite links:
#   https://t.me/channel_name
#   https://t.me/+InviteHash
#   http://t.me/...
#   t.me/...  (bare, not preceded by a letter/digit)
_TGLINK_RE = re.compile(
    r"https?://(?:www\.)?t\.me/[A-Za-z0-9_+\-/?&=#%.@]+"
    r"|(?<![A-Za-z0-9_\.])t\.me/[A-Za-z0-9_+\-/?&=#%.@]+",
    re.IGNORECASE,
)

# Bot watermarks to strip — standalone lines anywhere in the caption.
# Covers: "Kuttu bot™ Files", "✨ ZiZuBot™", "ZiZuBot™", and similar patterns.
_WATERMARK_RE = re.compile(
    r"(?im)"
    r"(?:^kuttu\s+bot[\u2122™]?\s*files[ \t]*\r?\n?)"   # Kuttu bot™ Files
    r"|(?:^[\u2728\u2b50\u2b55\U0001F300-\U0001FFFF ]*"  # leading emoji (✨ ⭐ etc)
    r"ziZuBot[\u2122™]?[ \t]*\r?\n?)"                    # ZiZuBot™
    r"|(?:^ziZuBot[\u2122™]?[ \t]*\r?\n?)",              # ZiZuBot™ without emoji
    re.UNICODE,
)


def _build_promo_re(patterns: list) -> "re.Pattern | None":
    """
    Compile a single regex that matches any full line containing one of the
    given keyword patterns.  Returns None if the list is empty.

    Each match covers the entire line (from start-of-line to the optional
    trailing newline) so no blank-line fragments are left behind.
    """
    if not patterns:
        return None
    # Wrap each user pattern so it anchors to a whole line (MULTILINE).
    # ^[^\n]* … [^\n]*\r?\n?  →  the entire line including its newline.
    line_pats = [rf"^[^\n]*(?:{p})[^\n]*\r?\n?" for p in patterns]
    combined  = "|".join(f"(?:{lp})" for lp in line_pats)
    return re.compile(combined, re.MULTILINE | re.IGNORECASE | re.UNICODE)


def _load_promo_re() -> "re.Pattern | None":
    """
    Build _PROMO_RE from config.STRIP_PATTERNS at import time.
    Falls back to no filtering if config is unavailable or has no STRIP_PATTERNS.
    """
    try:
        import config as _cfg
        patterns = getattr(_cfg, "STRIP_PATTERNS", [])
    except ImportError:
        patterns = []
    return _build_promo_re(patterns)


# Compiled once at import time from config.STRIP_PATTERNS.
# Matches any caption line that is pure channel promotion / watermark text
# (e.g. "Latest Movies - Master Print Downloader📂(MPD)5.0").
_PROMO_RE: "re.Pattern | None" = _load_promo_re()

# Map of common extension → extra mime substrings to check
_MIME_HINTS: dict[str, list[str]] = {
    "mkv":  ["matroska", "x-mkv"],
    "mp4":  ["mp4"],
    "avi":  ["avi", "x-msvideo"],
    "mov":  ["quicktime"],
    "mp3":  ["mpeg", "mp3"],
    "pdf":  ["pdf"],
    "zip":  ["zip"],
    "rar":  ["rar"],
    "epub": ["epub"],
}


def clean_caption(text: str, replacement: str) -> str:
    """
    Clean a caption before re-sending — four steps, always in this order:

      1. Strip bot-watermark lines (Kuttu bot™ Files, ✨ ZiZuBot™ …) — always.
      2. Strip promo/ad lines from config.STRIP_PATTERNS — always.
         Entire lines are removed, e.g.:
           "Latest Movies - Master Print Downloader📂(MPD)5.0"
           "Movie Request Group - MPD Requested Movies Zone 3.0📂"
           "🔗 CHANNEL LINK 👉 https://t.me/..."
      3. Replace every https://t.me/... and t.me/... link with *replacement*.
      4. Replace every remaining @username with *replacement*.
         Pass replacement="" or None to keep original links/usernames unchanged.

    Returns the cleaned text, stripped of leading/trailing whitespace.
    Consecutive blank lines left by stripping are collapsed to a single blank line.
    """
    if not text:
        return text

    # Step 1 — strip bot watermark lines
    text = _WATERMARK_RE.sub("", text)

    # Step 2 — strip promo / channel-ad lines (whole-line removal)
    if _PROMO_RE is not None:
        text = _PROMO_RE.sub("", text)

    # Collapse runs of 3+ newlines down to 2 (one blank line) so stripping
    # middle lines doesn't leave an ugly double-blank gap.
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Steps 3 & 4 — link and username replacement
    if replacement:
        # Replace t.me links FIRST (longer pattern, must come before @username)
        text = _TGLINK_RE.sub(replacement, text)
        # Replace remaining @username mentions
        text = _USERNAME_RE.sub(replacement, text)

    return text.strip()


def _is_deleted_or_empty(message) -> bool:
    """
    True for messages that should always be skipped regardless of other filters.

    Catches:
      1. Service messages — message.action is set (join/leave/pin/topic/etc.)
      2. Deleted-media placeholders — message.media is MessageMediaEmpty
      3. Completely empty — no text AND no usable media at all
    """
    # 1. Service / action messages
    if getattr(message, "action", None) is not None:
        return True

    media = getattr(message, "media", None)

    # 2. Deleted-media placeholder (MessageMediaEmpty)
    if media is not None:
        # Use the class name string to avoid a hard import at module level
        if type(media).__name__ == "MessageMediaEmpty":
            return True

    # 3. Completely empty — no text, no (real) media
    has_text  = bool(getattr(message, "message", None))
    has_media = bool(media)
    if not has_text and not has_media:
        return True

    return False


def matches_filter(
    message,
    allowed_exts: set[str],
    skip_text: bool = False,
) -> bool:
    """
    Return True if the message should be copied.

    Always skips (regardless of skip_text):
      • Service/action messages  (join, leave, pin, etc.)
      • Deleted-media placeholders  (MessageMediaEmpty)
      • Completely empty messages

    skip_text=False (default):
      Text-only posts like season labels, hashtags, quality info pass through.

    skip_text=True:
      Also skip text-only messages that have no media at all.
      Use this only if you explicitly want to drop ALL text.

    allowed_exts:
      If set, media must match by filename extension or mime type.
      Text-only messages that passed the skip_text check are always let through.
    """
    # Step 1 — always block deleted / empty / service messages
    if _is_deleted_or_empty(message):
        return False

    has_media = bool(getattr(message, "media", None))

    # Step 2 — optional blanket text filter
    if skip_text and not has_media:
        return False

    # Step 3 — no extension filter: pass everything remaining
    if not allowed_exts:
        return True

    # Step 4 — extension filter applies only to media messages
    if not has_media:
        return True  # text message reached here → skip_text=False, let it through

    file_obj = getattr(message, "file", None)

    # Check filename extension
    fname = getattr(file_obj, "name", None) or ""
    if fname:
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        if ext in allowed_exts:
            return True

    # Fallback: check mime type
    mime = (getattr(file_obj, "mime_type", None) or "").lower()
    for ext in allowed_exts:
        hints = _MIME_HINTS.get(ext, [ext])
        if any(h in mime for h in hints):
            return True

    return False


def parse_ext_filter(raw: str) -> set[str]:
    """
    Parse a comma-separated list like 'mkv,mp4,avi' into a lowercase set.
    Returns empty set (= no filter) for blank input.
    """
    if not raw or not raw.strip():
        return set()
    return {e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()}
