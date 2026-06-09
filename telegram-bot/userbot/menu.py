"""
Interactive terminal menu for the userbot.
"""
import logging
import os

from colorama import Fore, Style, init as colorama_init
from telethon import TelegramClient

from .forwarder import copy_channel_files, list_chats, dry_run as do_dry_run
from .sync import run_sync
from . import checkpoint as ckpt
from .filter_utils import parse_ext_filter
import config

colorama_init(autoreset=True)
logger = logging.getLogger(__name__)

CHECKPOINTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "checkpoints"
)

DEFAULT_REPLACEMENT = config.CAPTION_REPLACE or "@backupchannek"


def _header(title: str):
    print()
    print(Fore.CYAN + "="*54 + Style.RESET_ALL)
    print(Fore.CYAN + f"  {title}" + Style.RESET_ALL)
    print(Fore.CYAN + "="*54 + Style.RESET_ALL)


def _pending_checkpoints() -> list:
    if not os.path.exists(CHECKPOINTS_DIR):
        return []
    out = []
    for fname in os.listdir(CHECKPOINTS_DIR):
        if fname.endswith(".json"):
            parts = fname.replace(".json", "").split("_")
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                out.append(fname)
    return out


def _parse_chat(raw: str):
    """Parse user input into int ID or @username."""
    raw = raw.strip()
    if raw.startswith("@"):
        return raw
    digits = raw.lstrip("-")
    if digits.isdigit():
        return int(raw)
    return raw


def _ask_filter_options() -> tuple[set, str, int, bool]:
    """
    Ask user for:
      1. Skip text-only messages  (default = yes)
      2. File type filter         (e.g. mkv,mp4  or Enter = ALL)
      3. Username replacement     (default = @backupchannek, Enter to keep)
      4. Notification frequency   (e.g. 100  or Enter = 100  or 0 = off)

    Returns (allowed_exts: set, caption_replacement: str, notify_every: int, skip_text: bool)
    """
    print()
    print(Fore.YELLOW +
          "─── Filter & Caption Options ───────────────────────" +
          Style.RESET_ALL)

    # Skip text-only messages
    print(Fore.YELLOW +
          "Skip text-only messages? Season labels, hashtags, quality info = INCLUDED by default [y/N]:" +
          Style.RESET_ALL)
    print(Fore.CYAN +
          "  (Deleted/empty messages are ALWAYS blocked automatically)" +
          Style.RESET_ALL)
    skip_raw = input("  Skip text-only [N]: ").strip().lower()
    skip_text = skip_raw in ("y", "yes")
    if skip_text:
        print(Fore.GREEN + "  ✔ Text-only messages will be SKIPPED" + Style.RESET_ALL)
    else:
        print(Fore.YELLOW + "  ✔ Season labels, hashtags etc. will be INCLUDED (deleted messages still auto-blocked)" + Style.RESET_ALL)

    print()
    # File type filter
    print(Fore.YELLOW +
          "File type filter (e.g. mkv  or  mkv,mp4  or  Enter = ALL):" +
          Style.RESET_ALL)
    ext_raw = input("  Filter: ").strip()
    allowed_exts = parse_ext_filter(ext_raw)
    if allowed_exts:
        print(Fore.GREEN + f"  ✔ Only copying: {', '.join(sorted(allowed_exts)).upper()}" + Style.RESET_ALL)
    else:
        print(Fore.GREEN + "  ✔ Copying ALL file types" + Style.RESET_ALL)

    # Username replacement
    _def_repl = config.CAPTION_REPLACE or "(keep original)"
    print()
    print(Fore.YELLOW +
          f"Replace @usernames in captions with? (Enter = {_def_repl}):" +
          Style.RESET_ALL)
    repl_raw = input(f"  Replace with [{_def_repl}]: ").strip()
    caption_replacement = repl_raw if repl_raw else config.CAPTION_REPLACE
    if caption_replacement and not caption_replacement.startswith("@"):
        caption_replacement = "@" + caption_replacement
    if caption_replacement:
        print(Fore.GREEN + f"  ✔ Any @username → {caption_replacement}" + Style.RESET_ALL)
    else:
        print(Fore.GREEN + "  ✔ Keeping original @usernames" + Style.RESET_ALL)

    # Telegram progress notifications
    _def_notify = config.NOTIFY_EVERY
    _def_notify_lbl = str(_def_notify) if _def_notify else "0 (off)"
    print()
    print(Fore.YELLOW +
          f"Notify me in Telegram every N files copied? (Enter = {_def_notify_lbl}, 0 = off):" +
          Style.RESET_ALL)
    notify_raw = input(f"  Notify every [{_def_notify_lbl}]: ").strip()
    if notify_raw == "0":
        notify_every = 0
        print(Fore.YELLOW + "  ✔ Notifications OFF" + Style.RESET_ALL)
    else:
        notify_every = int(notify_raw) if notify_raw.isdigit() else _def_notify
        print(Fore.GREEN + f"  ✔ Telegram update every {notify_every} files → Saved Messages" + Style.RESET_ALL)

    return allowed_exts, caption_replacement, notify_every, skip_text


async def run_menu(client: TelegramClient):
    while True:
        pending = _pending_checkpoints()
        _header("MAIN MENU")
        print("  1.  Copy files (no forward tag)")
        print("  2.  Auto-sync new messages (live)")
        print("  3.  Dry run (preview only, nothing sent)")
        print("  4.  List my chats / channels (with IDs)")
        print(f"  5.  Resume saved job  ({len(pending)} pending)")
        print("  6.  Exit")
        print(Fore.CYAN + "="*54 + Style.RESET_ALL)

        choice = input("\n▶  Choose (1–6): ").strip()

        if choice == "1":
            await _copy_flow(client, dry_run_mode=False)
        elif choice == "2":
            await _sync_flow(client)
        elif choice == "3":
            await _copy_flow(client, dry_run_mode=True)
        elif choice == "4":
            await _list_chats_flow(client)
        elif choice == "5":
            await _resume_flow(client)
        elif choice == "6":
            print(Fore.GREEN + "\n👋  Goodbye!\n" + Style.RESET_ALL)
            await client.disconnect()
            break
        else:
            print(Fore.RED + "❌  Invalid choice." + Style.RESET_ALL)


async def _list_chats_flow(client: TelegramClient):
    _header("MY CHATS & CHANNELS")
    print(Fore.YELLOW + "⏳  Loading dialogs...\n" + Style.RESET_ALL)
    chats = await list_chats(client)
    print(f"{'ID':<22} {'Type':<13} {'Name'}")
    print("-" * 65)
    for c in chats:
        print(f"{str(c['id']):<22} {c['type']:<13} {c['name']}")
    print(f"\n{Fore.CYAN}Total: {len(chats)} dialogs{Style.RESET_ALL}")


async def _sync_flow(client: TelegramClient):
    _header("AUTO-SYNC (LIVE NEW MESSAGES)")
    print(Fore.YELLOW +
          "This watches the source channel and instantly copies every new\n"
          "message to your channel — WITHOUT the 'Forwarded from' tag.\n"
          "Press Ctrl+C to stop.\n"
          + Style.RESET_ALL)

    _def_src = str(config.SOURCE_CHANNEL) if config.SOURCE_CHANNEL else ""
    _def_dst = str(config.DEST_CHANNEL)   if config.DEST_CHANNEL   else ""
    _src_hint = f" [{_def_src}]" if _def_src else ""
    _dst_hint = f" [{_def_dst}]" if _def_dst else ""
    source_raw = input(f"SOURCE channel ID or @username{_src_hint}: ").strip() or _def_src
    dest_raw   = input(f"DESTINATION channel ID or @username{_dst_hint}: ").strip() or _def_dst

    source = _parse_chat(source_raw)
    dest   = _parse_chat(dest_raw)

    allowed_exts, caption_replacement, _, skip_text = _ask_filter_options()

    print(Fore.CYAN + f"\n⚙️   {source_raw}  →  {dest_raw}" + Style.RESET_ALL)
    confirm = input("Start auto-sync? (y/n): ").strip().lower()
    if confirm != "y":
        print(Fore.RED + "❌  Cancelled." + Style.RESET_ALL)
        return

    await run_sync(
        client, source, dest,
        allowed_exts=allowed_exts,
        caption_replacement=caption_replacement,
        skip_text=skip_text,
    )


async def _copy_flow(client: TelegramClient, dry_run_mode: bool = False):
    mode_label = "DRY RUN (PREVIEW)" if dry_run_mode else "COPY FILES (NO FORWARD TAG)"
    _header(mode_label)

    if dry_run_mode:
        print(Fore.YELLOW +
              "Dry run scans the source and tells you exactly what would be\n"
              "copied — no messages are actually sent.\n"
              + Style.RESET_ALL)
    else:
        print(Fore.YELLOW +
              "Messages are sent WITHOUT the 'Forwarded from' tag.\n"
              "Albums are grouped. Supports resume if interrupted.\n"
              "Press Ctrl+C at any time — progress is saved automatically.\n"
              + Style.RESET_ALL)

    _def_src = str(config.SOURCE_CHANNEL) if config.SOURCE_CHANNEL else ""
    _def_dst = str(config.DEST_CHANNEL)   if config.DEST_CHANNEL   else ""
    _src_hint = f" [{_def_src}]" if _def_src else ""
    _dst_hint = f" [{_def_dst}]" if _def_dst else ""
    source_raw = input(f"SOURCE channel ID or @username{_src_hint}: ").strip() or _def_src
    dest_raw   = input(f"DESTINATION channel ID or @username{_dst_hint}: ").strip() or _def_dst

    source = _parse_chat(source_raw)
    dest   = _parse_chat(dest_raw)

    limit_raw = input("Max messages? (Enter = ALL): ").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else None

    allowed_exts, caption_replacement, notify_every, skip_text = _ask_filter_options()

    if dry_run_mode:
        print(Fore.CYAN + f"\n🔍  Scanning {source_raw}..." + Style.RESET_ALL)
        await do_dry_run(
            client, source, dest,
            limit=limit,
            allowed_exts=allowed_exts,
            caption_replacement=caption_replacement,
            skip_text=skip_text,
        )
        return

    # Check for existing checkpoint
    src_id = abs(source) if isinstance(source, int) else 0
    dst_id = abs(dest)   if isinstance(dest,   int) else 0
    force_restart = False

    if src_id and dst_id and ckpt.exists(src_id, dst_id):
        state = ckpt.load(src_id, dst_id)
        print(
            Fore.YELLOW +
            f"\n♻️   Saved checkpoint found:\n"
            f"   Copied so far : {state['copied']:,}\n"
            f"   Last msg ID   : {state['last_msg_id']}\n"
            f"   Last updated  : {state.get('updated_at', '?')}"
            + Style.RESET_ALL
        )
        ch = input("Resume? (y = resume, n = start fresh): ").strip().lower()
        force_restart = (ch == "n")

    print()
    print(Fore.CYAN  + f"⚙️   {source_raw}  →  {dest_raw}" + Style.RESET_ALL)
    print(f"   Messages : {'ALL' if not limit else f'{limit:,}'}")
    print(f"   Mode     : {'FRESH START' if force_restart else 'RESUME'}")
    if allowed_exts:
        print(f"   Filter   : {', '.join(sorted(allowed_exts)).upper()} only")
    print(f"   Skip txt : {'YES' if skip_text else 'NO'}")
    print(f"   @... fix : {caption_replacement}")
    print(f"   Notify   : {'every ' + str(notify_every) + ' files' if notify_every else 'OFF'}")
    print(Fore.YELLOW + "   Ctrl+C = pause and save progress" + Style.RESET_ALL)

    confirm = input("\nConfirm? (y/n): ").strip().lower()
    if confirm != "y":
        print(Fore.RED + "❌  Cancelled." + Style.RESET_ALL)
        return

    await copy_channel_files(
        client,
        source,
        dest,
        limit=limit,
        force_restart=force_restart,
        dry_run_mode=False,
        allowed_exts=allowed_exts,
        caption_replacement=caption_replacement,
        notify_every=notify_every,
        skip_text=skip_text,
    )


async def _resume_flow(client: TelegramClient):
    _header("RESUME SAVED JOBS")
    pending = _pending_checkpoints()
    if not pending:
        print(Fore.YELLOW + "  No saved jobs." + Style.RESET_ALL)
        return

    jobs = []
    for i, fname in enumerate(pending, 1):
        parts = fname.replace(".json", "").split("_")
        src_id, dst_id = int(parts[0]), int(parts[1])
        state = ckpt.load(src_id, dst_id)
        print(
            f"  {i}. {src_id} → {dst_id} | "
            f"copied={state['copied']:,} | "
            f"last_id={state['last_msg_id']} | "
            f"updated={state.get('updated_at', '?')}"
        )
        jobs.append((src_id, dst_id))

    print(f"  {len(jobs)+1}. Back")
    sel = input("\n▶  Choose: ").strip()
    if not sel.isdigit() or int(sel) < 1 or int(sel) > len(jobs):
        return

    src_id, dst_id = jobs[int(sel) - 1]
    allowed_exts, caption_replacement, notify_every, skip_text = _ask_filter_options()
    await copy_channel_files(
        client, src_id, dst_id,
        force_restart=False,
        allowed_exts=allowed_exts,
        caption_replacement=caption_replacement,
        notify_every=notify_every,
        skip_text=skip_text,
    )
