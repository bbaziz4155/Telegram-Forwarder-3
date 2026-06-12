"""
Caption cleaning and file-type filtering utilities.

  clean_caption(text, replacement)        — swap every @username AND t.me link with replacement
  matches_filter(message, exts, skip_text) — True if message passes the filter
  reload_strip_patterns()                 — rebuild _PROMO_RE after patterns change at runtime

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
import json
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

# Path to user-managed patterns file (created by /strippatterns command)
_CUSTOM_PATTERNS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "strip_patterns.json"
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


def _all_patterns() -> list:
    """
    Return the merged list of built-in (config.py) + custom (JSON file) patterns.
    """
    try:
        import config as _cfg
        builtin = list(getattr(_cfg, "STRIP_PATTERNS", []))
    except ImportError:
        builtin = []

    try:
        with open(_CUSTOM_PATTERNS_FILE) as f:
            custom = json.load(f)
        if not isinstance(custom, list):
            custom = []
    except FileNotFoundError:
        custom = []
    except Exception:
        custom = []

    return builtin + custom


def _load_promo_re() -> "re.Pattern | None":
    """Build _PROMO_RE from all patterns at import time."""
    return _build_promo_re(_all_patterns())


# Compiled once at import time; call reload_strip_patterns() after any change.
_PROMO_RE: "re.Pattern | None" = _load_promo_re()


def reload_strip_patterns() -> int:
    """
    Rebuild _PROMO_RE from the current config + custom JSON file.
    Call this after adding or removing custom patterns via /strippatterns.
    Returns the total number of active patterns.
    """
    global _PROMO_RE
    patterns = _all_patterns()
    _PROMO_RE = _build_promo_re(patterns)
    return len(patterns)


def load_custom_patterns() -> list:
    """Return the list of user-added patterns from the JSON file."""
    try:
        with open(_CUSTOM_PATTERNS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_custom_patterns(patterns: list) -> None:
    """Persist the custom patterns list to disk."""
    os.makedirs(os.path.dirname(_CUSTOM_PATTERNS_FILE), exist_ok=True)
    with open(_CUSTOM_PATTERNS_FILE, "w") as f:
        json.dump(patterns, f, indent=2)


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
      2. Strip promo/ad lines from config.STRIP_PATTERNS + custom JSON — always.
         Entire lines are removed, e.g.:
           "Latest Movies - Master Print Downloader📂(MPD)5.0"
           "FILE ADDED BY GOUTHAM SER ❤️"
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
    if getattr(message, "action", None) is not None:
        return True

    media = getattr(message, "media", None)

    if media is not None:
        if type(media).__name__ == "MessageMediaEmpty":
            return True

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
    if _is_deleted_or_empty(message):
        return False

    has_media = bool(getattr(message, "media", None))

    if skip_text and not has_media:
        return False

    if not allowed_exts:
        return True

    if not has_media:
        return True

    file_obj = getattr(message, "file", None)

    fname = getattr(file_obj, "name", None) or ""
    if fname:
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        if ext in allowed_exts:
            return True

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
