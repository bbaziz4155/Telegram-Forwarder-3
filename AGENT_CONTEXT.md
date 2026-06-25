# Telegram Forwarder Bot — Agent Context File

> **Purpose of this file:** Any agent (human or AI) picking up this project can read this file and immediately understand what the bot does, what has already been built, what bugs were fixed, what features are pending, and exactly how the owner wants it to work.

---

## What this bot does

A **Telegram userbot + Telegram bot** that bulk-copies all messages from one Telegram channel to another **without** the "Forwarded from" tag. It uses:

- **Telethon** (userbot / client account) — to read messages from the source channel and send them to the destination channel as original posts
- **python-telegram-bot v21** (the bot itself) — for the admin UI (commands, buttons, progress messages)
- **Railway** — hosting/deployment platform (auto-deploys from GitHub on every push)
- **SQLite + aiosqlite** — local database for dedup tracking and session state

The owner has a source channel with **~830,000 messages** (mostly movie/series files) and wants to copy them all to a private destination channel.

---

## Repository structure

```
telegram-bot/           ← working directory (on sys.path at runtime)
  bot.py                ← entry point; registers all handlers; creates DB tables
  config.py             ← all env var reads + STRIP_PATTERNS watermark list
  database.py           ← SQLite helpers (aiosqlite); dedup table schema
  userbot/
    __init__.py         ← empty (makes userbot/ a package)
    bridge.py           ← manages Telethon client lifecycle
    forwarder.py        ← main copy engine (iter_messages → send)
    sender.py           ← sends individual messages/albums to dest
    checkpoint.py       ← saves/loads progress checkpoint JSON files
    filter_utils.py     ← caption cleaning: strips watermarks & promo lines
  handlers/
    copybot.py          ← ALL bot commands: /copy, /resume, /stopjob, /status,
                           /stats, /setcaption, /cleancaptions, /stopcleaning
    gensession.py       ← /gensession command (interactive session string gen)
    forward_rules.py    ← /addrule, /delrule live-forward rules
```

> **Critical import rule:** `telegram-bot/` has NO `__init__.py`, so it is NOT a Python package — it is just a directory on `sys.path`. Code inside `userbot/` must use **absolute imports** like `from database import ...` NOT relative imports like `from ..database import ...`. Relative imports will crash the bot at startup with `ImportError: attempted relative import beyond top-level package`.

---

## Environment variables (set in Railway)

| Variable | Description |
|---|---|
| `SESSION_STRING` | Base64-encoded Telethon session string. Generate with `/gensession` command. ⚠️ The code reads `SESSION_STRING` — NOT `TELETHON_SESSION`. Make sure Railway has this exact variable name. |
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `ADMIN_ID` | Your Telegram user ID (only this user can control the bot) |
| `SOURCE_CHANNEL` | Channel ID or username to copy FROM |
| `DEST_CHANNEL` | Channel ID or username to copy TO |
| `SESSION_SECRET` | Secret for PicklePersistence encryption |

---

## What has already been built / fixed

### Bug fixes (all pushed to GitHub, all deployed on Railway)

1. **`flood_sleep_threshold=0`** — Telethon was crashing on any flood wait instead of sleeping through it. Fixed in `bridge.py`.

2. **Checkpoint I/O explosion** — checkpoint was being saved after EVERY single message (disk thrash + slowdown). Fixed to save every 100 messages.

3. **Checkpoint error handling** — corrupt/missing checkpoint file crashed the bot. Added try/except with graceful fallback.

4. **Takeout session for `iter_messages`** — switched to Telethon Takeout API which has lower rate limits for bulk reading.

5. **CRITICAL: `from ..database import` crash** — the dedup feature introduced a relative import (`from ..database import`) in `forwarder.py`. Since `telegram-bot/` has no `__init__.py`, this crashes at startup with `ImportError`. Fixed to `from database import`.

6. **`caption_suffix` missing from resume opts** — when a job was resumed after a crash, the `caption_suffix` key was missing from the opts dict, causing a `KeyError`. Fixed by adding `"caption_suffix": ""` as default and `opts.setdefault(...)` in the resume callback.

7. **`caption_suffix` not saved in `autoresume.py`** — `save_resume()` persisted all other opts to `autoresume.json` but omitted `caption_suffix`. Result: `/setcaption` suffix was silently dropped whenever the bot auto-resumed after a process kill. Fixed by adding `"caption_suffix": opts.get("caption_suffix", "")` to the saved opts dict.

8. **`synctest_cmd` tested the wrong channels** — `/synctest` always probed `config.SOURCE_CHANNEL` / `config.DEST_CHANNEL` (hard-coded), even when the running `/sync` was started with different channels via the wizard. `_launch_job` now stores `active_sync_src` / `active_sync_dst` in `bot_data`, and `synctest_cmd` reads those with a fallback to config values.

9. **Stale `active_sync_src`/`active_sync_dst` after sync stops** — the two new bot_data keys were not cleaned up in `_run_sync`'s cancel and error handlers, leaving stale channel IDs that could mislead a subsequent `/synctest` call. Both handlers now pop the keys alongside the other sync keys.

10. **`/resume` config-defaults path ignored `/setcaption` suffix** — when `/resume` fell through to the config-defaults branch (no `autoresume.json` on disk, checkpoint file present), the `caption_suffix` the user had set via `/setcaption` was never applied. Fixed by adding `"caption_suffix": context.user_data.get("caption_suffix", "")` alongside the other config overrides.

### New features (all pushed to GitHub)

1. **Persistent dedup** (`database.py` + `forwarder.py`)
   - SQLite table `copied_files(id, source_chat_id, dest_chat_id, message_id, document_id, copied_at)`
   - Before copying any file, checks both `message_id` AND Telegram `document_id` against this table
   - Prevents re-sending files even if the bot is restarted or the checkpoint is lost
   - `document_id` dedup means the same physical file (same bytes) is never sent twice even if it appeared in multiple messages

2. **Watermark stripping** (`config.py` + `filter_utils.py`)
   - `STRIP_PATTERNS` list in `config.py` — regex patterns matched case-insensitively against each line of a caption
   - Currently strips: `"FILE ADDED BY GOUTHAM SER ❤️"`, `"Master Print Downloader"`, `"Movie Request Group"`, `"CHANNEL LINK"`
   - To add more: append a regex string to `STRIP_PATTERNS` in `config.py`

3. **`/setcaption` command** (`handlers/copybot.py` + `userbot/sender.py`)
   - `/setcaption <text>` — appends a custom line to the bottom of every copied caption going forward
   - `/setcaption off` — removes the suffix
   - `/setcaption` alone — shows the current suffix
   - Stored in `context.user_data["caption_suffix"]` (persisted via PicklePersistence)
   - Threaded through `forwarder.py` → `sender.py`

4. **`/cleancaptions` command** (`handlers/copybot.py`)
   - Scans ALL existing messages in `DEST_CHANNEL` and edits captions in-place to strip watermarks
   - Uses the same `clean_caption()` function as the copy engine — removes whatever is in `STRIP_PATTERNS`
   - Live progress: scanned / edited / skipped / errors, updates every 4 seconds
   - **Requires the userbot to be an admin with "Edit Messages" permission in the destination channel**
   - `/stopcleaning` cancels mid-run

---

  12. **Session revocation on Railway redeploy — graceful shutdown** — every time a new commit was pushed to GitHub, Railway would start a new container while the old one was still running. Telegram saw two simultaneous connections with the same session string and revoked one. The bot would restart with a dead session and show "⚠️ Session Revoked". Fixed in `bot.py`:
      - Added `post_shutdown(application)` async function that PTB v21 calls automatically when SIGTERM arrives (every Railway redeploy sends SIGTERM first).
      - On shutdown: (1) cancels active copy/sync/clean tasks, (2) cancels the userbot reconnect loop, (3) cleanly disconnects Telethon with a 10-second timeout.
      - Added `.post_shutdown(post_shutdown)` to the `Application.builder()` chain so PTB calls it on every clean exit.
      - No signal wiring needed — PTB v21's `run_polling()` handles SIGTERM natively.
  
## Pending features (NOT yet built — owner wants these)

### 1. Dual-userbot parallel copy (most wanted)

**Owner's idea:** Use TWO Telegram accounts simultaneously to double copy speed. When one account hits a 47-minute flood wait, the other keeps running.

**How to implement:**
- Add `TELETHON_SESSION_2` env var for the second account's session string
- Add a second Telethon client managed by `bridge.py`
- `/copy` gets a "Split mode" option — automatically divides the total message range in half
- Account A copies messages from ID 1 → midpoint
- Account B copies messages from midpoint+1 → end
- Both run as parallel `asyncio.Task`s inside the same process
- Both write to the **same** SQLite dedup table → zero duplicates guaranteed
- Progress shown as two live rows in one status message

**Anti-duplicate layers:**
1. Range split means they never touch the same message IDs
2. Shared `copied_files` table catches any edge-case overlap

### 2. Session revocation alert

When Telegram revokes the userbot session mid-copy (happens during long runs), the bot currently just stops silently. Owner discovers it hours later.

**Needed:** Detect `SessionRevokedError` / `AuthKeyUnregisteredError` in the copy loop and immediately send a Telegram message: "⚠️ Session revoked! Run /gensession then /resume."

### 3. Dedup count in `/stats`

The existing `/stats` command shows checkpoint-based job history but NOT the dedup table count.

**Needed:** Add a row to each per-channel-pair entry:
```
🛡 Dedup DB: 22,483 unique files tracked
```
One query: `SELECT COUNT(*) FROM copied_files WHERE source_chat_id=? AND dest_chat_id=?`

---

## How the copy engine works (for agents building on this)

```
/copy command
  → user picks source, dest, speed, filters via inline keyboard
  → _run_copy() starts as asyncio.Task
    → opens Telethon Takeout session
    → iter_messages(source, reverse=True, offset_id=checkpoint)
    → for each message:
        1. check copied_files table (msg_id + doc_id) → skip if found
        2. apply text filters (username replace, watermark strip)
        3. call sender.send_message() or sender.send_album()
        4. write to copied_files table
        5. update checkpoint every 100 messages
    → on FloodWaitError: sleep, notify user, auto-resume
    → on completion: send summary
```

`sender.py` handles the actual Telegram send:
- Single media: `client.send_file(dest, file, caption=cleaned_caption)`
- Albums (grouped messages): collects the group then sends as `client.send_file(dest, [files], caption=...)`
- Caption suffix appended at the very end before sending

---

## Known issues

- **`SESSION_STRING` not `TELETHON_SESSION`:** The bridge reads `os.environ.get("SESSION_STRING", "")`. The Railway variable MUST be named `SESSION_STRING` exactly. Using `TELETHON_SESSION` (a common mistake) means the bot starts without a session and never auto-connects. / sharp edges

- **Session revocations:** Telegram detects mass download/upload and revokes the session. Owner must regenerate with `/gensession`. Enabling 2FA on the userbot account reduces frequency. Running at "Fast" speed instead of "Turbo" also helps.

- **Railway deploys every commit:** When multiple commits are pushed quickly, Railway queues and deploys each one. Some intermediate commits may fail health checks if they contain startup bugs — Railway automatically rolls forward to the next commit. Wait for the latest commit to become ACTIVE.

- **`telegram-bot/` is the working directory, not a package.** Always use absolute imports inside `userbot/` when importing from `telegram-bot/` level (e.g., `from database import ...`, `from config import ...`).

- **Checkpoint count vs dedup:** The checkpoint stores the last processed message ID. The dedup table stores every successfully copied file. If the checkpoint is lost, the bot re-scans from 0 but the dedup table prevents re-sending. The `/stats` command shows checkpoint-based counts (which reset per-job), not the cumulative dedup table count.

- **830K messages takes days at any speed.** Flood waits of 47–49 minutes are normal at Turbo speed on a channel this size. The bot auto-resumes after each flood wait.

---

## Owner's goals (plain language)

1. Copy all ~830,000 files from source to destination channel as fast as possible
2. No "Forwarded from" tag on any copied message
3. No duplicate files — not even if the bot crashes and restarts
4. Strip "FILE ADDED BY GOUTHAM SER ❤️" and similar watermark text from captions
5. Eventually: two userbot accounts working in parallel to double speed
6. Everything managed through Telegram bot commands — no server access needed
