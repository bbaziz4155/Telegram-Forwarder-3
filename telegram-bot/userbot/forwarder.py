"""
Bulk copy engine with:
  - No "Forwarded from" tag — sends as own messages
  - Album / grouped-message support
  - File-type filter (e.g. only MKV)
  - Caption username replacement (e.g. @other → @backupchannek)
  - Checkpoint / resume
  - Live tqdm progress bar (copied, skipped, failed, flood waits)
  - Dry-run mode
"""
import asyncio
import logging
import sys
import time
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, User
from tqdm import tqdm
from colorama import Fore, Style, init as colorama_init

from . import checkpoint as ckpt
from .sender import send_album, _do_send
from .filter_utils import matches_filter
from .notifier import ProgressNotifier
from database import load_copied_ids, mark_copied_batch

colorama_init(autoreset=True)
logger = logging.getLogger(__name__)

RATE_DELAY = 0.0    # no artificial delay — push as fast as Telegram allows; FloodWait is the only throttle
SAVE_EVERY = 25     # save checkpoint every N messages


def _ok(m):   print(Fore.GREEN  + m + Style.RESET_ALL)
def _warn(m): print(Fore.YELLOW + m + Style.RESET_ALL)
def _err(m):  print(Fore.RED    + m + Style.RESET_ALL)
def _info(m): print(Fore.CYAN   + m + Style.RESET_ALL)


def _entity_id(entity) -> int:
    return abs(getattr(entity, "id", 0))


async def _count_messages(client: TelegramClient, entity) -> int:
    try:
        result = await client.get_messages(entity, limit=1)
        return result.total
    except Exception:
        return 0


def _update_pbar(pbar, copied, skipped, failed, flood_waits):
    pbar.set_postfix({
        "✅": copied,
        "⏭":  skipped,
        "❌": failed,
        "⏳": flood_waits,
    }, refresh=True)


# ─── public API ──────────────────────────────────────────────────────────────

async def list_chats(client: TelegramClient) -> list:
    result = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Channel):
            # Normalise supergroups → "group" so they appear in the chats list.
            # Broadcast channels keep their own "channel" type.
            ctype = "channel" if entity.broadcast else "group"
        elif isinstance(entity, Chat):
            ctype = "group"
        elif isinstance(entity, User):
            ctype = "bot" if entity.bot else "user"
        else:
            ctype = "unknown"
        result.append({"id": dialog.id, "name": dialog.name, "type": ctype})
    return result


async def dry_run(
    client: TelegramClient,
    source,
    dest,
    limit: int = None,
    allowed_exts: set = None,
    caption_replacement: str = "",
    skip_text: bool = False,
):
    """
    Scan source and report what WOULD be copied — no messages are sent.
    Respects the file-type filter.
    """
    try:
        source_entity = await client.get_entity(source)
        dest_entity   = await client.get_entity(dest)
    except Exception as e:
        _err(f"❌  Could not resolve chat: {e}")
        return

    source_name = getattr(source_entity, "title", str(source))
    dest_name   = getattr(dest_entity,   "title", str(dest))

    _info("\n🔍  DRY RUN — nothing will be sent")
    _info(f"📡  Source      : {source_name}")
    _info(f"📥  Destination : {dest_name}")
    if allowed_exts:
        _info(f"🔎  File filter : {', '.join(sorted(allowed_exts)).upper()} only")
    if skip_text:
        _info("🚫  Text-only msgs: SKIPPED")
    if caption_replacement:
        _info(f"✏️   Username fix : @... → {caption_replacement}")

    total = await _count_messages(client, source_entity)
    if limit:
        total = min(total, limit)
    _info(f"📊  Total messages in source: {total:,}\n")

    text_count    = 0
    media_count   = 0
    album_count   = 0
    skip_count    = 0
    filtered_out  = 0
    current_group = None
    group_msgs: list = []

    pbar = tqdm(total=total, unit="msg", colour="cyan", dynamic_ncols=True, disable=not sys.stdout.isatty())
    try:
        async for msg in client.iter_messages(source_entity, limit=limit, reverse=True):
            if msg.grouped_id:
                if msg.grouped_id != current_group:
                    # flush previous group
                    if current_group is not None and group_msgs:
                        passes = any(matches_filter(m, allowed_exts, skip_text=False) for m in group_msgs)
                        if passes:
                            album_count += 1
                        else:
                            filtered_out += 1
                    current_group = msg.grouped_id
                    group_msgs = [msg]
                else:
                    group_msgs.append(msg)
            else:
                # flush any pending album
                if current_group is not None and group_msgs:
                    passes = any(matches_filter(m, allowed_exts, skip_text=False) for m in group_msgs)
                    if passes:
                        album_count += 1
                    else:
                        filtered_out += 1
                    current_group = None
                    group_msgs = []

                if not matches_filter(msg, allowed_exts, skip_text=skip_text):
                    filtered_out += 1
                elif msg.media:
                    media_count += 1
                elif msg.message:
                    text_count += 1
                else:
                    skip_count += 1

            pbar.update(1)

        # flush last group
        if current_group is not None and group_msgs:
            passes = any(matches_filter(m, allowed_exts, skip_text=False) for m in group_msgs)
            if passes:
                album_count += 1
            else:
                filtered_out += 1

    except KeyboardInterrupt:
        pass

    pbar.close()

    total_to_copy = media_count + album_count + text_count
    print()
    print(Fore.CYAN + "="*54 + Style.RESET_ALL)
    print(Fore.CYAN + "  🔍  DRY RUN RESULTS" + Style.RESET_ALL)
    print(Fore.CYAN + "="*54 + Style.RESET_ALL)
    print(Fore.GREEN  + f"  Single media msgs : {media_count:,}"   + Style.RESET_ALL)
    print(Fore.GREEN  + f"  Albums (grouped)  : {album_count:,}"   + Style.RESET_ALL)
    print(Fore.GREEN  + f"  Text-only msgs    : {text_count:,}"    + Style.RESET_ALL)
    print(Fore.YELLOW + f"  Filtered out      : {filtered_out:,}"  + Style.RESET_ALL)
    print(Fore.YELLOW + f"  Empty/service     : {skip_count:,}"    + Style.RESET_ALL)
    print(Fore.GREEN  + f"  TOTAL to copy     : {total_to_copy:,}" + Style.RESET_ALL)
    print(Fore.CYAN + "="*54 + Style.RESET_ALL)
    print()


async def dry_run_results(
    client: TelegramClient,
    source,
    dest,
    limit: int = None,
    allowed_exts: set = None,
    caption_replacement: str = "",
    skip_text: bool = False,
) -> dict | None:
    """
    Scan source and return a stats dict — does NOT print anything.
    Returns None if the channels cannot be resolved.
    Used by the Telegram bot handler so results can be sent as a message.
    """
    try:
        source_entity = await client.get_entity(source)
        dest_entity   = await client.get_entity(dest)
    except Exception as e:
        logger.warning(f"dry_run_results: could not resolve chat: {e}")
        return None

    source_name = getattr(source_entity, "title", str(source))
    dest_name   = getattr(dest_entity,   "title", str(dest))

    total = await _count_messages(client, source_entity)
    if limit:
        total = min(total, limit)

    text_count    = 0
    media_count   = 0
    album_count   = 0
    skip_count    = 0
    filtered_out  = 0
    current_group = None
    group_msgs: list = []

    try:
        async for msg in client.iter_messages(source_entity, limit=limit, reverse=True):
            if msg.grouped_id:
                if msg.grouped_id != current_group:
                    if current_group is not None and group_msgs:
                        passes = any(matches_filter(m, allowed_exts, skip_text=False) for m in group_msgs)
                        if passes:
                            album_count += 1
                        else:
                            filtered_out += 1
                    current_group = msg.grouped_id
                    group_msgs = [msg]
                else:
                    group_msgs.append(msg)
            else:
                if current_group is not None and group_msgs:
                    passes = any(matches_filter(m, allowed_exts, skip_text=False) for m in group_msgs)
                    if passes:
                        album_count += 1
                    else:
                        filtered_out += 1
                    current_group = None
                    group_msgs = []

                if not matches_filter(msg, allowed_exts, skip_text=skip_text):
                    filtered_out += 1
                elif msg.media:
                    media_count += 1
                elif msg.message:
                    text_count += 1
                else:
                    skip_count += 1

        if current_group is not None and group_msgs:
            passes = any(matches_filter(m, allowed_exts, skip_text=False) for m in group_msgs)
            if passes:
                album_count += 1
            else:
                filtered_out += 1

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"dry_run_results scan error: {e}")

    return {
        "source_name":    source_name,
        "dest_name":      dest_name,
        "media":          media_count,
        "albums":         album_count,
        "text":           text_count,
        "filtered":       filtered_out,
        "empty":          skip_count,
        "total_to_copy":  media_count + album_count + text_count,
        "total_scanned":  total,
    }


async def copy_channel_files(
    client: TelegramClient,
    source,
    dest,
    limit: int = None,
    force_restart: bool = False,
    dry_run_mode: bool = False,
    allowed_exts: set = None,
    caption_replacement: str = "",
    caption_suffix: str = "",
    notify_every: int = 0,
    skip_text: bool = False,
    notifier: "ProgressNotifier | None" = None,
    interactive: bool = True,
    rate_delay: float = RATE_DELAY,
    min_id: int = None,
    max_id: int = None,
):
    """
    Copy all messages from source → dest without "Forwarded from" tag.
    - allowed_exts      : set of lowercase extensions like {'mkv','mp4'} or None for all
    - caption_replacement: replace every @username in captions with this string
    - notify_every      : send a Telegram update to Saved Messages every N copied files
                          (0 = disabled)
    - skip_text         : skip text-only messages (no media) — default True
    Albums are sent as grouped posts. Checkpoint saved every SAVE_EVERY messages.
    """
    # ── resolve entities ─────────────────────────────────────────────────────
    try:
        source_entity = await client.get_entity(source)
        dest_entity   = await client.get_entity(dest)
    except Exception as e:
        _err(f"❌  Could not resolve chat: {e}")
        _info("Tip: For private channels use the full numeric ID e.g. -1001234567890")
        raise  # propagate so the bot handler can report the error to the user

    source_id   = _entity_id(source_entity)
    dest_id     = _entity_id(dest_entity)
    source_name = getattr(source_entity, "title", str(source))
    dest_name   = getattr(dest_entity,   "title", str(dest))

    print()
    _info(f"📡  Source      : {source_name}  (id={source_id})")
    _info(f"📥  Destination : {dest_name}  (id={dest_id})")
    if allowed_exts:
        _info(f"🔎  File filter : {', '.join(sorted(allowed_exts)).upper()} only")
    if skip_text:
        _info("🚫  Text-only msgs: SKIPPED")
    if caption_replacement:
        _info(f"✏️   Username fix : @... → {caption_replacement}")
    if dry_run_mode:
        _warn("🔍  DRY RUN MODE — nothing will actually be sent")

    # ── checkpoint ────────────────────────────────────────────────────────────
    if force_restart:
        ckpt.delete(source_id, dest_id)
        _warn("🔄  Starting fresh.")

    state       = ckpt.load(source_id, dest_id)
    resume_from = state["last_msg_id"]
    # If an explicit min_id is provided (e.g. dual-copy worker scope), use it
    if min_id is not None:
        resume_from = min_id

    # Load persistent dedup sets from SQLite (survives job restarts / re-runs).
    # db_msg_ids  — source message IDs already copied in ANY previous run
    # db_doc_ids  — Telegram document IDs already copied (catches re-uploads)
    db_msg_ids, db_doc_ids = await load_copied_ids(source_id, dest_id)
    state["copied_ids"].update(db_msg_ids)  # merge with checkpoint gap-IDs
    _pending_db: list = []  # (msg_id, doc_id) batch for DB flush

    # ── destination pre-scan ──────────────────────────────────────────────────
    # Scan what files are ALREADY in the destination so we never re-send them
    # even when the SQLite dedup DB has been wiped (e.g. every Railway redeploy
    # creates a fresh container). Uses (filename, filesize) as the dedup key.
    # A 5-minute timeout prevents hanging on very large destination channels.
    dest_file_keys: set = set()
    _PRESCAN_TIMEOUT = 300  # seconds
    _info("🔍 Pre-scanning destination for existing files (prevents duplicates on redeploy)…")
    _info("   ℹ️  Bot message will show scan progress. Use /stopjob to skip.")
    async def _run_prescan():
        _fcount = 0   # files with a named attachment (what matters)
        _last_notify = 0
        async for _dm in client.iter_messages(dest_entity, reverse=False):
            _df = getattr(_dm, "file", None)
            if _df and getattr(_df, "name", None) and getattr(_df, "size", None):
                dest_file_keys.add((_df.name, _df.size))
                _fcount += 1
            # notify every 5,000 files found (not every 5,000 total messages)
            if _fcount > 0 and _fcount % 5000 == 0 and _fcount != _last_notify:
                _last_notify = _fcount
                _info(f"   … {_fcount:,} files found in destination ({len(dest_file_keys):,} unique)")
                if notifier is not None:
                    await notifier.scan_progress(_fcount, len(dest_file_keys))
    try:
        await asyncio.wait_for(_run_prescan(), timeout=_PRESCAN_TIMEOUT)
        _info(f"📦 Destination scan done: {len(dest_file_keys):,} unique files already there.")
    except asyncio.TimeoutError:
        _warn(f"⏱ Destination pre-scan timed out after {_PRESCAN_TIMEOUT // 60}m — "
              f"continuing with {len(dest_file_keys):,} files found so far.")
        if notifier is not None:
            await notifier.scan_progress(-1, len(dest_file_keys))
    except asyncio.CancelledError:
        raise  # propagate — user ran /stopjob
    except Exception as _de:
        logger.warning("Destination pre-scan aborted (%s) — continuing without it", _de)
        dest_file_keys = set()
    if resume_from > 0 and not force_restart:
        _ok(f"♻️   Resuming from msg ID {resume_from} "
            f"(already copied: {state['copied']:,})")
    else:
        _info("🆕  Starting from the beginning.")

    # ── total count ───────────────────────────────────────────────────────────
    _info("⏳  Counting messages...")
    total = await _count_messages(client, source_entity)
    already_done = resume_from
    remaining = max(0, total - already_done) if resume_from else total
    if limit:
        remaining = min(remaining, limit)
    _info(f"📊  Messages in source: {total:,}  |  Remaining: {remaining:,}\n")

    # ── counters ──────────────────────────────────────────────────────────────
    copied      = state["copied"]
    skipped     = state["skipped"]
    failed      = state["failed"]
    flood_waits = state.get("flood_waits", 0)
    processed   = 0
    last_save   = time.time()

    # ── progress bar ──────────────────────────────────────────────────────────
    pbar = tqdm(
        total=remaining,
        unit="msg",
        dynamic_ncols=True,
        colour="green" if not dry_run_mode else "cyan",
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        disable=not interactive,
    )
    _update_pbar(pbar, copied, skipped, failed, flood_waits)

    # ── telegram progress notifier ────────────────────────────────────────────
    if notifier is None:
        notifier = ProgressNotifier(client, every=notify_every)
        if notify_every > 0 and not dry_run_mode:
            _info(f"🔔  Telegram notifications every {notify_every} files → Saved Messages")

    # ── flood-wait wrapper: increments counter AND notifies bot ────────────────────
    async def _on_flood(secs: int):
        nonlocal flood_waits
        flood_waits += 1
        await notifier.flood_wait(secs)

    # ── album buffer ──────────────────────────────────────────────────────────
    album_buf: dict = {}
    album_order: list = []

    async def _flush_album(gid):
        nonlocal copied, failed, skipped
        msgs = album_buf.pop(gid, [])
        if gid in album_order:
            album_order.remove(gid)
        n = len(msgs)
        if not msgs:
            return n
        # Skip the whole album if every message was already copied
        all_already_done = all(m.id in state["copied_ids"] for m in msgs)
        # Also skip if every media file in the album is already in the destination
        # by filename+size (catches re-deploys where SQLite was wiped).
        if not all_already_done and dest_file_keys:
            _album_media = [m for m in msgs
                            if getattr(getattr(m, "file", None), "name", None)
                            and getattr(getattr(m, "file", None), "size", None)]
            if _album_media and all(
                (m.file.name, m.file.size) in dest_file_keys for m in _album_media
            ):
                all_already_done = True
        if all_already_done:
            skipped += n
            return n
        # Filter: if NO message in album passes, skip the whole album
        if not any(matches_filter(m, allowed_exts, skip_text=False) for m in msgs):
            skipped += n
            return n
        result = await send_album(
            client, dest_entity, msgs,
            dry_run=dry_run_mode,
            caption_replacement=caption_replacement,
            caption_suffix=caption_suffix,
            on_flood_wait=_on_flood,
        )
        if result == "ok":
            copied += n
            # Track highest msg id and mark all album IDs as done
            state["last_msg_id"] = max(state["last_msg_id"], msgs[-1].id)
            for m in msgs:
                state["copied_ids"].add(m.id)
                _doc = getattr(getattr(m, "file", None), "id", None)
                if _doc:
                    db_doc_ids.add(_doc)
                _pending_db.append((m.id, _doc))
                # Track in dest_file_keys so same file isn't sent again this run
                _fn = getattr(getattr(m, "file", None), "name", None)
                _fs = getattr(getattr(m, "file", None), "size", None)
                if _fn and _fs:
                    dest_file_keys.add((_fn, _fs))
            await notifier.tick(copied, skipped, failed, total, source_name, dest_name)
        elif result == "fail":
            failed += n
        else:
            skipped += n
        return n

    # ── main loop ─────────────────────────────────────────────────────────────
    # Attempt a Takeout session for the read phase — Telegram's bulk-export
    # API has lower rate limits and supports wait_time=0, which eliminates
    # flood waits during iter_messages.  Falls back silently to the normal
    # client if takeout is unavailable (permission delay, unsupported DC, etc.)
    # Sending always uses the main client — takeout is read-only.
    from telethon.errors import TakeoutInitDelayError as _TakeoutDelay
    _takeout_mgr  = None
    _iter_client  = client
    _iter_extra: dict = {}
    try:
        _takeout_mgr = client.takeout(channels=True, files=True, finalize=True)
        _iter_client = await _takeout_mgr.__aenter__()
        _iter_extra  = {"wait_time": 0}
        logger.info("Takeout session active — using wait_time=0 for iter_messages")
    except _TakeoutDelay as _e:
        logger.warning("Takeout delayed %ds — using normal session", _e.seconds)
        _takeout_mgr = None
    except Exception as _e:
        logger.warning("Takeout init failed (%s) — using normal session", _e)
        _takeout_mgr = None

    try:
        async for message in _iter_client.iter_messages(
            source_entity,
            limit=limit,
            min_id=resume_from,
            max_id=max_id if max_id is not None else 0,
            reverse=True,
            **_iter_extra,
        ):
            gid = message.grouped_id

            if gid:
                # Buffer this album message; flush any OTHER pending albums
                if gid not in album_buf:
                    album_buf[gid] = []
                    album_order.append(gid)
                album_buf[gid].append(message)

                for old_gid in list(album_order):
                    if old_gid != gid:
                        n = len(album_buf.get(old_gid, []))  # count BEFORE pop
                        await _flush_album(old_gid)
                        pbar.update(n or 1)
                        _update_pbar(pbar, copied, skipped, failed, flood_waits)

                # Pure buffering — no send yet; skip rate-limit sleep
                processed += 1
                now = time.time()
                if processed % SAVE_EVERY == 0 or (now - last_save) > 30:
                    state.update({"copied": copied, "skipped": skipped,
                                  "failed": failed, "flood_waits": flood_waits})
                    ckpt.save(source_id, dest_id, state)
                    last_save = now
                    await mark_copied_batch(source_id, dest_id, _pending_db)
                    _pending_db.clear()
                await asyncio.sleep(0)  # yield without rate-limit delay
                continue

            else:
                # Flush any pending albums before processing this single message
                for old_gid in list(album_order):
                    n = len(album_buf.get(old_gid, []))  # count BEFORE pop
                    await _flush_album(old_gid)
                    pbar.update(n or 1)
                    _update_pbar(pbar, copied, skipped, failed, flood_waits)

                # Duplicate check — skip if already copied in a previous run.
                # Three layers:
                #   1. message ID — same source message already processed
                #   2. document ID — same file re-uploaded at a different msg ID
                #   3. filename+size — already present in destination (survives
                #      Railway redeploys that wipe the SQLite DB)
                _msg_doc_id = getattr(getattr(message, "file", None), "id", None)
                _msg_fname  = getattr(getattr(message, "file", None), "name", None)
                _msg_fsize  = getattr(getattr(message, "file", None), "size", None)
                _msg_fkey   = (_msg_fname, _msg_fsize) if _msg_fname and _msg_fsize else None
                if (message.id in state["copied_ids"]
                        or (_msg_doc_id and _msg_doc_id in db_doc_ids)
                        or (_msg_fkey and _msg_fkey in dest_file_keys)):
                    skipped += 1
                    pbar.update(1)
                    _update_pbar(pbar, copied, skipped, failed, flood_waits)
                    processed += 1
                    await asyncio.sleep(0)
                    continue

                # File-type filter for single messages
                if not matches_filter(message, allowed_exts, skip_text=skip_text):
                    skipped += 1
                    pbar.update(1)
                    _update_pbar(pbar, copied, skipped, failed, flood_waits)
                    processed += 1
                    await asyncio.sleep(0)
                    continue

                result = await _do_send(
                    client, dest_entity, message,
                    dry_run=dry_run_mode,
                    caption_replacement=caption_replacement,
                    caption_suffix=caption_suffix,
                    on_flood_wait=_on_flood,
                )
                if result == "ok":
                    copied += 1
                    state["last_msg_id"] = message.id
                    state["copied_ids"].add(message.id)
                    if _msg_doc_id:
                        db_doc_ids.add(_msg_doc_id)
                    if _msg_fkey:
                        dest_file_keys.add(_msg_fkey)  # prevent re-send same file this run
                    _pending_db.append((message.id, _msg_doc_id))
                elif result == "skip":
                    skipped += 1
                else:
                    failed += 1

                pbar.update(1)
                _update_pbar(pbar, copied, skipped, failed, flood_waits)

            processed += 1

            # Progress notification — only for sent single messages (albums notify inside _flush_album)
            await notifier.tick(copied, skipped, failed, total,
                                source_name, dest_name)

            now = time.time()
            if processed % SAVE_EVERY == 0 or (now - last_save) > 30:
                state.update({"copied": copied, "skipped": skipped,
                              "failed": failed, "flood_waits": flood_waits})
                ckpt.save(source_id, dest_id, state)
                last_save = now
                await mark_copied_batch(source_id, dest_id, _pending_db)
                _pending_db.clear()

            await asyncio.sleep(rate_delay() if callable(rate_delay) else rate_delay)

        for gid in list(album_order):
            n = len(album_buf.get(gid, []))  # count BEFORE pop
            await _flush_album(gid)
            pbar.update(n or 1)
            _update_pbar(pbar, copied, skipped, failed, flood_waits)

    except KeyboardInterrupt:
        for gid in list(album_order):
            msgs = album_buf.get(gid, [])
            if msgs:
                state["last_msg_id"] = msgs[0].id
        pbar.close()
        print()
        _warn("⛔  Paused — progress saved. Resume any time.")
        state.update({"copied": copied, "skipped": skipped,
                      "failed": failed, "flood_waits": flood_waits})
        ckpt.save(source_id, dest_id, state)
        _print_summary(copied, skipped, failed, flood_waits, done=False)
        return

    except Exception as e:
        pbar.close()
        _err(f"\n❌  Unexpected error: {e}")
        logger.exception("Copy loop error")
        state.update({"copied": copied, "skipped": skipped,
                      "failed": failed, "flood_waits": flood_waits})
        ckpt.save(source_id, dest_id, state)
        raise  # propagate to _run_copy so it can update the bot message
    finally:
        # Clean up Takeout session regardless of how the loop exits
        if _takeout_mgr is not None:
            try:
                await _takeout_mgr.__aexit__(None, None, None)
            except Exception:
                pass

    pbar.close()

    # Final flush of any pending dedup records to SQLite
    await mark_copied_batch(source_id, dest_id, _pending_db)
    _pending_db.clear()

    state.update({"copied": copied, "skipped": skipped,
                  "failed": failed, "flood_waits": flood_waits})
    ckpt.save(source_id, dest_id, state)
    _print_summary(copied, skipped, failed, flood_waits, done=True)
    await notifier.done(copied, skipped, failed, total, source_name, dest_name)

    if not dry_run_mode and interactive:
        ask = input("🗑  Mark job as done and delete checkpoint? (y/n): ").strip().lower()
        if ask == "y":
            ckpt.delete(source_id, dest_id)
            _ok("✅  Checkpoint deleted.")


def _print_summary(copied, skipped, failed, flood_waits, done: bool):
    tag = "✅  COMPLETE" if done else "⏸  PAUSED"
    print()
    print(Fore.CYAN   + "="*54 + Style.RESET_ALL)
    print(Fore.CYAN   + f"  {tag}"                     + Style.RESET_ALL)
    print(Fore.CYAN   + "="*54                         + Style.RESET_ALL)
    print(Fore.GREEN  + f"  Sent (no fwd tag) : {copied:,}"  + Style.RESET_ALL)
    print(Fore.YELLOW + f"  Skipped/filtered  : {skipped:,}" + Style.RESET_ALL)
    print(Fore.RED    + f"  Failed            : {failed:,}"  + Style.RESET_ALL)
    print(Fore.YELLOW + f"  Flood waits       : {flood_waits:,}" + Style.RESET_ALL)
    print(Fore.CYAN   + "="*54 + Style.RESET_ALL)
    print()
