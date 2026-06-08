"""
Core send engine — always sends WITHOUT the "Forwarded from" tag.
Handles single messages, albums, text, files, and FloodWait auto-retry.
Applies caption cleaning (username replacement) before every send.

FloodWait policy: ALWAYS wait and retry — never mark a message as failed
due to rate-limiting alone. This allows Turbo mode to push at full speed
and let Telegram's own limits be the only throttle.
"""
import asyncio
import logging
from collections import defaultdict

from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaWebPage,
    MessageMediaPoll,
    MessageMediaGame,
    MessageMediaInvoice,
)
from telethon.errors import (
    FloodWaitError,
    ChannelPrivateError,
    ChatWriteForbiddenError,
    MediaEmptyError,
    FileReferenceExpiredError,
)
from colorama import Fore, Style

from .filter_utils import clean_caption

logger = logging.getLogger(__name__)

MAX_RETRIES    = 3     # retries for non-FloodWait errors only
ALBUM_FLUSH_DELAY = 0.6


def _ok(m):   print(Fore.GREEN  + m + Style.RESET_ALL)
def _warn(m): print(Fore.YELLOW + m + Style.RESET_ALL)
def _err(m):  print(Fore.RED    + m + Style.RESET_ALL)


def _is_sendable_media(media) -> bool:
    if media is None:
        return False
    unsendable = (MessageMediaWebPage, MessageMediaPoll,
                  MessageMediaGame, MessageMediaInvoice)
    return not isinstance(media, unsendable)


def _cleaned(text: str, replacement: str) -> str:
    return clean_caption(text or "", replacement)


async def _do_send(
    client: TelegramClient,
    dest,
    message,
    dry_run: bool = False,
    caption_replacement: str = "",
    on_flood_wait=None,
) -> str:
    """
    Send a single message to dest WITHOUT forwarding (no "Forwarded from" tag).
    Returns: 'ok' | 'skip' | 'fail'

    FloodWait is ALWAYS waited out and retried — it never counts as a failure.
    """
    if dry_run:
        logger.debug(f"[DRY-RUN] Would send msg {message.id}")
        return "ok"

    has_media = message.media and _is_sendable_media(message.media)
    has_text  = bool(message.message)

    if not has_media and not has_text:
        if message.media is not None:
            return "skip_unsupported"
        return "skip_deleted"

    caption  = _cleaned(message.message, caption_replacement)
    attempts = 0

    while True:
        try:
            if has_media:
                await client.send_file(
                    dest,
                    file=message.media,
                    caption=caption,
                    parse_mode="md",
                    force_document=False,
                )
            else:
                await client.send_message(dest, caption, parse_mode="md")
            return "ok"

        except FloodWaitError as fw:
            # Always wait — Telegram is telling us to slow down, not to stop.
            wait = fw.seconds + 2
            _warn(f"\n⏳  Flood wait {fw.seconds}s — pausing and retrying…")
            if on_flood_wait:
                try:
                    await on_flood_wait(fw.seconds)
                except Exception:
                    pass
            await asyncio.sleep(wait)

        except FileReferenceExpiredError:
            try:
                refreshed = await client.get_messages(message.chat_id, ids=message.id)
                if refreshed and refreshed.media:
                    cap = _cleaned(refreshed.message, caption_replacement)
                    await client.send_file(dest, file=refreshed.media,
                                           caption=cap, parse_mode="md",
                                           force_document=False)
                    return "ok"
            except Exception:
                pass
            return "fail"

        except MediaEmptyError:
            if has_text:
                try:
                    await client.send_message(dest, caption)
                    return "ok"
                except Exception:
                    pass
            return "fail"

        except (ChannelPrivateError, ChatWriteForbiddenError) as e:
            _err(f"\n❌  Permission error on destination: {e}")
            return "fail"

        except Exception as e:
            attempts += 1
            if attempts >= MAX_RETRIES:
                logger.warning(f"Msg {message.id} failed after {attempts} attempts: {e}")
                return "fail"
            await asyncio.sleep(1.5)


async def send_album(
    client: TelegramClient,
    dest,
    messages: list,
    dry_run: bool = False,
    caption_replacement: str = "",
    on_flood_wait=None,
) -> str:
    """
    Send a grouped album as a single post.
    FloodWait is ALWAYS waited out and retried.
    Returns: 'ok' | 'skip' | 'fail'
    """
    if not messages:
        return "skip"

    if dry_run:
        logger.debug(f"[DRY-RUN] Would send album of {len(messages)} messages")
        return "ok"

    files = []
    raw_caption = ""
    for msg in messages:
        if msg.media and _is_sendable_media(msg.media):
            files.append(msg.media)
        if msg.message:
            raw_caption = msg.message

    caption = _cleaned(raw_caption, caption_replacement)

    if not files:
        for msg in messages:
            if msg.message:
                try:
                    await client.send_message(dest, _cleaned(msg.message, caption_replacement))
                    return "ok"
                except Exception:
                    pass
        return "skip"

    attempts = 0
    while True:
        try:
            if len(files) == 1:
                await client.send_file(dest, file=files[0], caption=caption, parse_mode="md")
            else:
                await client.send_file(dest, file=files, caption=caption, parse_mode="md")
            return "ok"

        except FloodWaitError as fw:
            _warn(f"\n⏳  Flood wait {fw.seconds}s (album) — pausing and retrying…")
            if on_flood_wait:
                try:
                    await on_flood_wait(fw.seconds)
                except Exception:
                    pass
            await asyncio.sleep(fw.seconds + 2)

        except FileReferenceExpiredError:
            try:
                ids       = [m.id for m in messages]
                refreshed = await client.get_messages(messages[0].chat_id, ids=ids)
                files     = [m.media for m in refreshed
                             if m and m.media and _is_sendable_media(m.media)]
                if files:
                    await client.send_file(dest, file=files, caption=caption, parse_mode="md")
                    return "ok"
            except Exception:
                pass
            return "fail"

        except Exception as e:
            attempts += 1
            if attempts >= MAX_RETRIES:
                logger.warning(f"Album send failed after {attempts} attempts: {e}")
                return "fail"
            await asyncio.sleep(1.5)
