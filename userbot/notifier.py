"""
Progress notifications — sends a Telegram message to your Saved Messages
(or any target chat) every N files copied so you can track progress on
your phone without watching the terminal.
"""
import time
import logging

from telethon import TelegramClient

logger = logging.getLogger(__name__)


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


class ProgressNotifier:
    """
    Call .tick(copied, skipped, failed, total, source_name, dest_name)
    after each message. Sends a Telegram notification every `every` copied
    messages, plus a final summary when .done() is called.

    Set every=0 to disable notifications entirely.
    """

    def __init__(
        self,
        client: TelegramClient,
        every: int = 100,
        target: str | int = "me",
    ):
        self.client      = client
        self.every       = every          # notify every N *copied* messages
        self.target      = target         # "me" = Saved Messages
        self._last_notify = 0             # last copied count at which we notified
        self._started     = time.time()

    def _elapsed(self) -> float:
        return time.time() - self._started

    def _eta(self, copied: int, total: int) -> str:
        if copied == 0 or total == 0:
            return "?"
        elapsed = self._elapsed()
        rate = copied / elapsed          # msgs per second
        remaining = max(0, total - copied)
        return _fmt_time(remaining / rate) if rate > 0 else "?"

    async def tick(
        self,
        copied: int,
        skipped: int,
        failed: int,
        total: int,
        source_name: str = "",
        dest_name: str = "",
        duplicates: int = 0,
        deleted: int = 0,
        non_media: int = 0,
        unsupported: int = 0,
    ):
        if self.every <= 0:
            return
        if copied == 0:
            return
        if copied - self._last_notify < self.every:
            return

        self._last_notify = copied
        pct = int(copied / total * 100) if total else 0
        bar_len = 10
        filled  = int(bar_len * pct / 100)
        bar     = "█" * filled + "░" * (bar_len - filled)

        text = (
            f"📊 **Copy Progress**\n"
            f"`[{bar}] {pct}%`\n\n"
            f"✅ Saved                    : `{copied:,}` / `{total:,}`\n"
            f"♻️ Duplicates skipped       : `{duplicates:,}`\n"
            f"🗑 Deleted msgs skipped     : `{deleted:,}`\n"
            f"🚫 Non-media skipped        : `{non_media:,}` (Unsupported: `{unsupported:,}`)\n"
            f"⚠️ Errors                   : `{failed:,}`\n"
            f"⏱ Elapsed  : `{_fmt_time(self._elapsed())}`\n"
            f"⏳ ETA      : `{self._eta(copied, total)}`\n"
        )
        if source_name:
            text += f"\n📡 `{source_name}` → `{dest_name}`"

        try:
            await self.client.send_message(self.target, text, parse_mode="md")
        except Exception as e:
            logger.warning(f"Notifier: could not send progress message: {e}")

    async def flood_wait(self, seconds: int):
        """Called when a FloodWaitError occurs. Override to notify the user."""
        pass

    async def done(
        self,
        copied: int,
        skipped: int,
        failed: int,
        total: int,
        source_name: str = "",
        dest_name: str = "",
        duplicates: int = 0,
        deleted: int = 0,
        non_media: int = 0,
        unsupported: int = 0,
    ):
        if self.every <= 0:
            return
        elapsed = _fmt_time(self._elapsed())
        status  = "✅ **Indexing Complete!**" if failed == 0 else "⚠️ **Copy Finished (with errors)**"
        text = (
            f"{status}\n\n"
            f"✅ Saved: `{copied:,}`\n"
            f"♻️ Duplicates skipped: `{duplicates:,}`\n"
            f"🗑 Deleted messages skipped: `{deleted:,}`\n"
            f"🚫 Non-media skipped: `{non_media:,}` (Unsupported: `{unsupported:,}`)\n"
            f"⚠️ Errors: `{failed:,}`\n"
            f"⏱ Total time: `{elapsed}`\n"
        )
        if source_name:
            text += f"\n📡 `{source_name}` → `{dest_name}`"
        try:
            await self.client.send_message(self.target, text, parse_mode="md")
        except Exception as e:
            logger.warning(f"Notifier: could not send done message: {e}")
