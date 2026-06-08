import logging
from telegram import Update
from telegram.ext import ContextTypes
import database as db

logger = logging.getLogger(__name__)

async def load_rules_on_startup(bot_data: dict):
    """Load all active rules and ignore lists from DB into memory on startup."""
    rules = await db.get_all_active_rules()
    bot_data["forward_rules"] = {}
    for r in rules:
        key = (r["source_chat_id"], r["dest_chat_id"])
        bot_data["forward_rules"][key] = {
            "rule_id":     r["id"],
            "user_id":     r["user_id"],
            "source_name": r["source_chat_name"],
            "dest_name":   r["dest_chat_name"],
        }
    logger.info(f"Loaded {len(rules)} forward rules from DB")

    # Load ignore lists per user so handle_forward can check them
    ignore_rows = await db.get_all_ignore_entries()
    ignore_map: dict[int, set[int]] = {}
    for row in ignore_rows:
        ignore_map.setdefault(row["user_id"], set()).add(row["chat_id"])
    bot_data["ignore_map"] = ignore_map
    logger.info(f"Loaded ignore entries for {len(ignore_map)} user(s)")


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler that fires on every message — checks if source chat has a rule."""
    message = update.message or update.channel_post
    if not message:
        return

    source_id  = message.chat_id
    rules      = context.bot_data.get("forward_rules", {})
    ignore_map = context.bot_data.get("ignore_map", {})

    for (src, dst), meta in list(rules.items()):
        if src != source_id:
            continue

        # Skip if the rule owner has this source chat in their ignore list
        owner_id      = meta.get("user_id")
        ignored_chats = ignore_map.get(owner_id, set())
        if source_id in ignored_chats:
            logger.debug(
                f"Skipping msg {message.message_id} from {source_id} — "
                f"in ignore list of user {owner_id}"
            )
            continue

        try:
            await context.bot.forward_message(
                chat_id=dst,
                from_chat_id=source_id,
                message_id=message.message_id,
            )
            logger.info(
                f"Forwarded msg {message.message_id} from "
                f"{meta['source_name']} to {meta['dest_name']}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to forward msg {message.message_id} "
                f"from {src} to {dst}: {e}"
            )
