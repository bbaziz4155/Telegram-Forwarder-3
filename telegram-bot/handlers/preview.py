"""
Caption preview — /previewcaption  +  /striptest

/previewcaption
    Enter preview mode.  Send any file, text, or forwarded channel post and
    the bot replies with both the original and the cleaned caption so you can
    verify exactly what /copy and /sync will produce.

/striptest <replacement>
    (Inside preview mode only)  Temporarily override the replacement string
    for this session — no need to touch config.py.

    /striptest @OtherChannel       use @OtherChannel as the replacement
    /striptest https://t.me/ch     use a URL
    /striptest empty               strip usernames/links with no replacement
    /striptest reset               restore the config.py / active-sync default

Replacement priority (highest first):
  1. Explicit /striptest override stored in user_data["preview_replacement"]
  2. Active /sync or /copy job opts  (bot_data["active_sync_opts"])
  3. config.CAPTION_REPLACE          (global default)
"""
import logging
import re

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from userbot.filter_utils import clean_caption
from states import PREVIEW_AWAIT_MSG

logger = logging.getLogger(__name__)

# Must match filter_utils._USERNAME_RE and _TGLINK_RE exactly so that
# _describe_changes() reports the same usernames/links that clean_caption()
# actually replaces.
_USERNAME_PAT = re.compile(r"@[A-Za-z0-9_]{3,32}")
_TGLINK_PAT   = re.compile(
    r"https?://(?:www\.)?t\.me/[A-Za-z0-9_+\-/?&=#%.@]+"
    r"|(?<![A-Za-z0-9_\.])t\.me/[A-Za-z0-9_+\-/?&=#%.@]+",
    re.IGNORECASE,
)

# Cap each block so the combined message stays under Telegram's 4096-char limit.
_MAX_BLOCK    = 600
_MAX_REPL_LEN = 64     # guard against absurdly long /striptest values

# user_data key that holds the /striptest override
_UKEY = "preview_replacement"

# Keywords that mean "use empty string (strip all, no replacement)"
_EMPTY_KEYWORDS = {"empty", "none", "off", "clear", "no", "strip"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _active_replacement(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Return the currently active replacement string using the priority chain:
      1. /striptest override (user_data)
      2. Active sync/copy job opts (bot_data)
      3. config.CAPTION_REPLACE
    Handles empty-string override correctly — does NOT collapse "" to the default.
    """
    if _UKEY in context.user_data:
        return context.user_data[_UKEY]          # explicit override (may be "")
    opts = context.bot_data.get("active_sync_opts") or {}
    if "caption_replacement" in opts:
        return opts["caption_replacement"]        # running job's setting
    return config.CAPTION_REPLACE                 # global fallback


def _repl_hint(replacement: str, source: str = "") -> str:
    """Human-readable label for the current replacement."""
    label = f"`{replacement}`" if replacement else "_strip (no replacement)_"
    if source:
        label += f"  _{source}_"
    return label


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _safe(text: str) -> str:
    """
    Sanitise text for a triple-backtick Markdown code block.
    Triple backticks or lone backticks would close the block early.
    """
    return text.replace("```", "'''").replace("`", "'")


def _describe_changes(original: str, cleaned: str, replacement: str) -> list[str]:
    """
    Human-readable list of what clean_caption() actually changed.
    Mirrors the logic in filter_utils.clean_caption step-by-step.
    """
    changes: list[str] = []

    # Step 1 & 2 — removed lines (watermark / promo strip)
    orig_lines  = original.splitlines()
    clean_lines = cleaned.splitlines()
    removed     = max(0, len(orig_lines) - len(clean_lines))
    if removed:
        changes.append(f"{removed} watermark/promo line{'s' if removed > 1 else ''} stripped")

    # Steps 3 & 4 — username / link handling
    orig_users  = set(_USERNAME_PAT.findall(original))
    orig_links  = _TGLINK_PAT.findall(original)

    if replacement:
        # Find usernames present in original but absent from cleaned.
        # Comparing sets avoids counting the replacement itself (which may be
        # an @username) as something that was "replaced".
        clean_users = set(_USERNAME_PAT.findall(cleaned))
        replaced    = orig_users - clean_users
        if replaced:
            listed = ", ".join(sorted(replaced)[:4])
            suffix = " …" if len(replaced) > 4 else ""
            s      = "s" if len(replaced) > 1 else ""
            changes.append(f"Username{s} {listed}{suffix} → `{replacement}`")

        clean_links = _TGLINK_PAT.findall(cleaned)
        link_delta  = max(0, len(orig_links) - len(clean_links))
        if link_delta:
            s = "s" if link_delta > 1 else ""
            changes.append(f"{link_delta} t.me link{s} → `{replacement}`")
    else:
        # replacement == "" → clean_caption skips steps 3 & 4 entirely,
        # so usernames and links are preserved as-is.  Tell the user explicitly
        # instead of leaving the change list empty.
        if orig_users:
            listed = ", ".join(sorted(orig_users)[:4])
            suffix = " …" if len(orig_users) > 4 else ""
            changes.append(f"@username{listed}{suffix} kept (no replacement set)")
        if orig_links:
            changes.append(f"{len(orig_links)} t.me link(s) kept (no replacement set)")

    if not changes:
        changes.append("Caption modified (whitespace / formatting adjusted)")

    return changes


# ── entry point ───────────────────────────────────────────────────────────────

async def previewcaption_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Clear any stale /striptest override from a previous session.
    # allow_reentry=True resets the conv state but not user_data — we must
    # clear manually so re-entry always starts from the current active default.
    context.user_data.pop(_UKEY, None)

    replacement = _active_replacement(context)
    source      = "(from active job)" if context.bot_data.get("active_sync_opts") else ""
    hint        = _repl_hint(replacement, source)

    await update.message.reply_text(
        f"🔍 *Caption Preview*\n\n"
        f"Send me any file, text, or forward a channel post — I'll show exactly "
        f"what the caption looks like after cleaning.\n\n"
        f"Current replacement: {hint}\n\n"
        f"💡 Use `/striptest @Other` to test a different replacement without "
        f"touching config.py.\n"
        f"_Send /cancel to exit._",
        parse_mode="Markdown",
    )
    return PREVIEW_AWAIT_MSG


# ── /striptest ────────────────────────────────────────────────────────────────

async def striptest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Change the replacement string for this preview session only."""
    msg  = update.message
    args = context.args or []

    if not args:
        current = _active_replacement(context)
        await msg.reply_text(
            f"*Usage:*\n"
            f"`/striptest @Channel`  — test with @Channel as replacement\n"
            f"`/striptest https://t.me/ch`  — test with a URL\n"
            f"`/striptest empty`  — strip usernames/links, no replacement\n"
            f"`/striptest reset`  — restore default\n\n"
            f"Current: {_repl_hint(current)}",
            parse_mode="Markdown",
        )
        return PREVIEW_AWAIT_MSG

    raw = " ".join(args).strip()

    # ── reset keyword ─────────────────────────────────────────────────────────
    if raw.lower() == "reset":
        context.user_data.pop(_UKEY, None)
        default = _active_replacement(context)   # now reads from opts/config
        await msg.reply_text(
            f"♻️ Replacement reset to default: {_repl_hint(default)}\n"
            f"_Send a message to preview._",
            parse_mode="Markdown",
        )
        return PREVIEW_AWAIT_MSG

    # ── empty/strip-all keyword ───────────────────────────────────────────────
    if raw.lower() in _EMPTY_KEYWORDS:
        context.user_data[_UKEY] = ""
        await msg.reply_text(
            "✂️ Replacement cleared — @usernames and t.me links will be *kept* "
            "as-is (no substitution).\n"
            "_Send a message to preview._",
            parse_mode="Markdown",
        )
        return PREVIEW_AWAIT_MSG

    # ── validation ────────────────────────────────────────────────────────────
    if "\\" in raw:
        await msg.reply_text(
            "⚠️ Replacement must not contain backslashes "
            "(they'd be treated as regex escape codes)."
        )
        return PREVIEW_AWAIT_MSG

    if len(raw) > _MAX_REPL_LEN:
        await msg.reply_text(
            f"⚠️ Replacement is too long ({len(raw)} chars). "
            f"Max is {_MAX_REPL_LEN} chars."
        )
        return PREVIEW_AWAIT_MSG

    # ── store override ────────────────────────────────────────────────────────
    context.user_data[_UKEY] = raw
    await msg.reply_text(
        f"✅ Replacement set to {_repl_hint(raw)} for this session.\n"
        f"_Send a message to preview, or `/striptest reset` to restore the default._",
        parse_mode="Markdown",
    )
    return PREVIEW_AWAIT_MSG


# ── main message handler ──────────────────────────────────────────────────────

async def preview_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg      = update.message
    original = msg.caption or msg.text or ""

    if not original:
        await msg.reply_text(
            "⚠️ No caption or text found in that message.\n"
            "Send a file with a caption or a plain text message.",
        )
        return PREVIEW_AWAIT_MSG

    replacement = _active_replacement(context)
    cleaned     = clean_caption(original, replacement)

    orig_block  = _truncate(original, _MAX_BLOCK)
    clean_block = _truncate(cleaned,  _MAX_BLOCK)

    # Show which replacement is in effect so the user knows what they're testing
    source = ""
    if _UKEY in context.user_data:
        source = "via /striptest"
    elif context.bot_data.get("active_sync_opts"):
        source = "from active job"
    repl_label = _repl_hint(replacement, source)

    if original == cleaned:
        body = (
            f"*📝 Caption (unchanged):*\n"
            f"```\n{_safe(orig_block)}\n```\n\n"
            f"✅ _No changes — already clean._"
        )
    else:
        changes     = _describe_changes(original, cleaned, replacement)
        change_text = "\n".join(f"  • {c}" for c in changes)
        body = (
            f"*📝 Original:*\n"
            f"```\n{_safe(orig_block)}\n```\n\n"
            f"*✂️ Cleaned:*\n"
            f"```\n{_safe(clean_block)}\n```\n\n"
            f"*Changes made:*\n{change_text}"
        )

    footer = (
        f"\n\n_Replacement: {repl_label}_\n"
        f"_Send another or /cancel to exit._"
    )
    text = f"🔍 *Caption Preview*\n\n{body}{footer}"

    # Hard cap — guard against edge-case overflow
    if len(text) > 4000:
        text = text[:3990] + "…`"

    await msg.reply_text(text, parse_mode="Markdown")
    return PREVIEW_AWAIT_MSG


# ── cancel ────────────────────────────────────────────────────────────────────

async def preview_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Always clean up the /striptest override so it doesn't bleed into the
    # next session (even though previewcaption_cmd also clears it on re-entry).
    context.user_data.pop(_UKEY, None)
    await update.message.reply_text("✅ Caption preview closed.")
    return ConversationHandler.END


# ── conversation builder ──────────────────────────────────────────────────────

def build_preview_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("previewcaption", previewcaption_cmd)],
        states={
            PREVIEW_AWAIT_MSG: [
                # /striptest MUST be listed before the catch-all MessageHandler
                # so PTB routes it to striptest_cmd, not preview_message.
                # (The MessageHandler uses ~filters.COMMAND so it would NOT catch
                # commands anyway, but explicit ordering is clearer and safer.)
                CommandHandler("striptest", striptest_cmd),
                MessageHandler(filters.ALL & ~filters.COMMAND, preview_message),
            ],
        },
        fallbacks=[CommandHandler("cancel", preview_cancel)],
        per_user=True,
        per_chat=False,
        per_message=False,
        allow_reentry=True,
        name="preview_conv",
    )
