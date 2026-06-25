"""
Auto-sync mode — watches a source channel for NEW messages and instantly
copies them to the destination WITHOUT the "Forwarded from" tag.
Handles single messages and albums (grouped posts).
Applies caption username replacement (e.g. @other → @backupchannek).
Runs until the user presses Ctrl+C.
"""
import asyncio
import logging
import time

from telethon import TelegramClient, events
from colorama import Fore, Style, init as colorama_init

from .sender import send_album, _do_send
from .filter_utils import matches_filter

colorama_init(autoreset=True)
logger = logging.getLogger(__name__)

ALBUM_WAIT = 1.2   # seconds to wait for album siblings before flushing


def _ok(m):   print(Fore.GREEN  + f"[sync] {m}" + Style.RESET_ALL)
def _warn(m): print(Fore.YELLOW + f"[sync] {m}" + Style.RESET_ALL)
def _err(m):  print(Fore.RED    + f"[sync] {m}" + Style.RESET_ALL)
def _info(m): print(Fore.CYAN   + f"[sync] {m}" + Style.RESET_ALL)


async def start_sync_handler(
    client: TelegramClient,
    source,
    dest,
    allowed_exts: set = None,
    caption_replacement: str = "",
    caption_suffix: str = "",
    skip_text: bool = False,
    on_forwarded=None,
):
    """
    Register the sync event handler WITHOUT blocking.
    Returns (handler_function, stats_dict, source_name, dest_name).

    The handler is active immediately after this call returns.
    To stop: client.remove_event_handler(handler_function)
    Used by the Telegram bot so sync runs alongside bot polling.

    on_forwarded: optional async callable(msg, result, stats) called after each
    send attempt (result is "ok", "skip", or "fail").  Used by the bot to push
    live progress updates into the Telegram chat.
    """
    source_entity = await client.get_entity(source)
    dest_entity   = await client.get_entity(dest)
    source_id   = source_entity.id
    source_name = getattr(source_entity, "title", str(source))
    dest_name   = getattr(dest_entity,   "title", str(dest))

    stats     = {"copied": 0, "failed": 0, "skipped": 0, "last_msg": None}
    album_buf: dict = {}

    async def _notify(msg, result):
        if on_forwarded:
            try:
                await on_forwarded(msg, result, stats)
            except Exception as e:
                logger.debug("on_forwarded callback error: %s", e)

    async def _flush_album(gid):
        entry = album_buf.pop(gid, None)
        if not entry:
            return
        msgs = entry["messages"]
        if not any(matches_filter(m, allowed_exts, skip_text=False) for m in msgs):
            stats["skipped"] += len(msgs)
            return
        result = await send_album(
            client, dest_entity, msgs,
            caption_replacement=caption_replacement,
            caption_suffix=caption_suffix,
        )
        if result == "ok":
            stats["copied"] += len(msgs)
            stats["last_msg"] = msgs[-1]
            await _notify(msgs[-1], "ok")
        elif result == "skip":
            stats["skipped"] += len(msgs)   # bug-fix: skip ≠ fail
        else:
            stats["failed"] += len(msgs)
            await _notify(msgs[-1], "fail")

    async def _schedule_flush(gid):
        await asyncio.sleep(ALBUM_WAIT)
        if gid in album_buf:
            await _flush_album(gid)

    @client.on(events.NewMessage(chats=source_id))
    async def sync_handler(event):
        msg = event.message
        gid = msg.grouped_id
        if gid:
            if gid not in album_buf:
                album_buf[gid] = {"messages": [], "task": None}
            album_buf[gid]["messages"].append(msg)
            old_task = album_buf[gid].get("task")
            if old_task and not old_task.done():
                old_task.cancel()
            album_buf[gid]["task"] = asyncio.create_task(_schedule_flush(gid))
        else:
            if not matches_filter(msg, allowed_exts, skip_text=skip_text):
                stats["skipped"] += 1
                return
            result = await _do_send(
                client, dest_entity, msg,
                caption_replacement=caption_replacement,
                caption_suffix=caption_suffix,
            )
            if result == "ok":
                stats["copied"] += 1
                stats["last_msg"] = msg
                await _notify(msg, "ok")
            elif result == "skip":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1
                await _notify(msg, "fail")

    logger.info(f"[sync] Handler registered: {source_name} → {dest_name}")
    return sync_handler, stats, source_name, dest_name


async def run_sync(
    client: TelegramClient,
    source,
    dest,
    allowed_exts: set = None,
    caption_replacement: str = "",
    skip_text: bool = False,
):
    """
    Start auto-sync: new messages in `source` are immediately re-sent to `dest`.
    - allowed_exts        : set of lowercase extensions e.g. {'mkv'} or None for all
    - caption_replacement : replace every @username in captions with this string
    Press Ctrl+C to stop.
    """
    try:
        source_entity = await client.get_entity(source)
        dest_entity   = await client.get_entity(dest)
    except Exception as e:
        _err(f"Could not resolve chat: {e}")
        return

    source_id   = source_entity.id
    source_name = getattr(source_entity, "title", str(source))
    dest_name   = getattr(dest_entity,   "title", str(dest))

    stats = {"copied": 0, "failed": 0, "skipped": 0}
    started = time.time()

    # Album buffer: grouped_id → {messages, flush_task}
    album_buf: dict = {}

    print()
    print(Fore.CYAN + "="*54 + Style.RESET_ALL)
    print(Fore.CYAN + "  🔄  AUTO-SYNC ACTIVE" + Style.RESET_ALL)
    print(Fore.CYAN + f"  📡  {source_name}  →  {dest_name}" + Style.RESET_ALL)
    if allowed_exts:
        print(Fore.CYAN + f"  🔎  Filter : {', '.join(sorted(allowed_exts)).upper()} only" + Style.RESET_ALL)
    if skip_text:
        print(Fore.CYAN + "  🚫  Text-only msgs: SKIPPED" + Style.RESET_ALL)
    if caption_replacement:
        print(Fore.CYAN + f"  ✏️   @... → {caption_replacement}" + Style.RESET_ALL)
    print(Fore.CYAN + "  Press Ctrl+C to stop." + Style.RESET_ALL)
    print(Fore.CYAN + "="*54 + Style.RESET_ALL)
    print()

    async def _flush_album(gid):
        entry = album_buf.pop(gid, None)
        if not entry:
            return
        msgs = entry["messages"]
        # Filter whole album: skip if no item passes
        if not any(matches_filter(m, allowed_exts, skip_text=False) for m in msgs):
            stats["skipped"] += len(msgs)
            _warn(f"Album filtered out ({len(msgs)} items)")
            return
        result = await send_album(
            client, dest_entity, msgs,
            caption_replacement=caption_replacement,
        )
        if result == "ok":
            stats["copied"] += len(msgs)
            _ok(f"Album sent ({len(msgs)} items) ✅")
        elif result == "skip":
            stats["skipped"] += len(msgs)   # bug-fix: skip ≠ fail
            _warn(f"Album skipped ({len(msgs)} items)")
        else:
            stats["failed"] += len(msgs)
            _err(f"Album failed ({len(msgs)} items)")

    async def _schedule_flush(gid):
        """Wait ALBUM_WAIT seconds then flush — allows sibling messages to arrive."""
        await asyncio.sleep(ALBUM_WAIT)
        if gid in album_buf:
            await _flush_album(gid)

    @client.on(events.NewMessage(chats=source_id))
    async def handler(event):
        msg = event.message
        gid = msg.grouped_id

        if gid:
            if gid not in album_buf:
                album_buf[gid] = {"messages": [], "task": None}
            album_buf[gid]["messages"].append(msg)

            old_task = album_buf[gid].get("task")
            if old_task and not old_task.done():
                old_task.cancel()
            album_buf[gid]["task"] = asyncio.create_task(_schedule_flush(gid))
        else:
            # File-type filter
            if not matches_filter(msg, allowed_exts, skip_text=skip_text):
                stats["skipped"] += 1
                _warn(f"Filtered out msg_id={msg.id}")
                return

            result = await _do_send(
                client, dest_entity, msg,
                caption_replacement=caption_replacement,
            )
            if result == "ok":
                stats["copied"] += 1
                mtype = "📎 file" if msg.media else "💬 text"
                _ok(f"Sent {mtype} (msg_id={msg.id}) ✅")
            elif result == "skip":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1
                _err(f"Failed to send msg_id={msg.id}")

    _info("Listening for new messages...")

    try:
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        pass
    finally:
        for gid, entry in list(album_buf.items()):
            t = entry.get("task")
            if t and not t.done():
                t.cancel()
            await _flush_album(gid)

        elapsed = int(time.time() - started)
        mins, secs = divmod(elapsed, 60)
        print()
        print(Fore.CYAN + "="*54 + Style.RESET_ALL)
        print(Fore.CYAN + "  🛑  AUTO-SYNC STOPPED" + Style.RESET_ALL)
        print(Fore.CYAN + "="*54 + Style.RESET_ALL)
        print(Fore.GREEN  + f"  Sent    : {stats['copied']:,}"  + Style.RESET_ALL)
        print(Fore.RED    + f"  Failed  : {stats['failed']:,}"  + Style.RESET_ALL)
        print(Fore.YELLOW + f"  Skipped : {stats['skipped']:,}" + Style.RESET_ALL)
        print(Fore.CYAN   + f"  Runtime : {mins}m {secs}s"     + Style.RESET_ALL)
        print(Fore.CYAN + "="*54 + Style.RESET_ALL)
        print()
