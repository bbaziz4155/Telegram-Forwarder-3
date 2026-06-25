# Telegram-Forwarder-3 — Agent Session Context

> **Purpose:** Complete working memory for any future AI agent continuing work on this repo.
> Last updated: 2026-06-25

---

## Project Summary

A Telegram **userbot + bot hybrid** that bulk-copies ~830K messages from a source channel
to a destination channel without "Forwarded from" tags. Hosted on **Railway**.

Two processes run together:
| Layer | Library | Role |
|---|---|---|
| **Bot** | python-telegram-bot v21 | Admin interface (commands, menus, auth guard) |
| **Userbot** | Telethon | Does the actual message reading + sending |

The bot controls the userbot via `userbot_bridge.py` — a thin wrapper that stores the
Telethon `TelegramClient` and a ready-flag in `bot_data`.

---

## Key Files

```
telegram-bot/
├── main.py               # Entrypoint — starts both bot + userbot
├── bot.py                # PTB Application setup, all handler registration, post_shutdown
├── config.py             # All env-var defaults (channels, API keys, strip patterns…)
├── database.py           # PostgreSQL via asyncpg — rules, ignore list, copy history
├── states.py             # ConversationHandler state constants
├── userbot_bridge.py     # get_client(), is_ready(), is_locked() — the bridge API
├── channel_settings.py   # Per-channel settings (rate delay, ext filter) persisted to JSON
│
├── handlers/
│   ├── autoresume.py     # Auto-resume file I/O: save_resume(), clear_resume(),
│   │                     # load_resume(), claim_resume() — uses DATA_DIR
│   ├── copybot.py        # All copy/dryrun/sync/resume/speed/status commands (~2900 lines)
│   │                     # schedule_auto_resume() uses claim_resume() (NOT load_resume())
│   ├── menu.py           # Main menu keyboard
│   ├── login.py          # /login OTP flow
│   ├── restart.py        # /restart — owner-only graceful process restart via os.execv()
│   ├── setchannel.py     # /setsrc /setdst commands
│   ├── purgedups.py      # /purgedups — dedup scan+delete
│   ├── strippatterns.py  # /strippatterns conversation
│   ├── channelinfo.py    # /channelinfo command
│   ├── admin_mgmt.py     # Admin allow/deny management
│   ├── history.py        # /forwardhistory — copy last N msgs from any channel pair
│   ├── rules.py          # Auto-forward rules (DB-backed)
│   └── ignore.py         # Ignore-list management (DB-backed)
│
└── userbot/
    ├── forwarder.py       # copy_channel_files() — the main bulk-copy engine
    ├── sender.py          # _do_send(), send_album() — low-level single-message send
    ├── sync.py            # start_sync_handler() — live sync event handler
    ├── checkpoint.py      # Checkpoint load/save/clear — uses DATA_DIR
    └── filter_utils.py    # clean_caption(), matches_filter(), reload_strip_patterns()
```

---

## Environment Variables (Railway)

| Var | Purpose |
|---|---|
| `API_ID` / `API_HASH` | Telegram API credentials |
| `BOT_TOKEN` | python-telegram-bot token |
| `SESSION_SECRET` | Fernet key for Telethon session encryption |
| `SOURCE_CHANNEL` | Default source channel ID (int) |
| `DEST_CHANNEL` | Default destination channel ID (int) |
| `DATA_DIR` | **Critical** — path for all persistent files (checkpoints, resume, strip_patterns.json, channel_settings.json). Must point to a Railway Volume mount or data is lost on redeploy. |
| `DATABASE_URL` | PostgreSQL connection string |
| `CAPTION_REPLACE` | @username to replace in captions |
| `CAPTION_SUFFIX` | Watermark text appended to every caption |
| `STRIP_PATTERNS` | JSON list of regex patterns to strip from captions |
| `ALLOWED_EXTS` | Comma-separated file extensions (blank = all) |
| `SKIP_TEXT` | "true" = skip text-only messages |
| `NOTIFY_EVERY` | Progress notification interval (0 = off) |
| `ADMIN_IDS` | Comma-separated Telegram user IDs allowed to use the bot |

---

## Architecture Decisions

1. **`interactive=False`** is always passed to `copy_channel_files()` from the bot — prevents
   any `input()` call from blocking the async event loop (confirmed line 857 of forwarder.py).

2. **Resume file** (`autoresume.json`) stores `{src, dst, last_id, opts}`. On an unexpected
   crash (`_clear_resume = False`) the file is kept so `autoresume.py` restarts the job on
   next bot boot. On `/stopjob` or successful completion (`_clear_resume = True`) it is
   deleted. Railway process kills do NOT call the finally block — file always survives kills.

3. **Bridge pattern**: The userbot Telethon client lives in `bot_data["_userbot_client"]`.
   Always access it via `bridge.get_client(bot_data)` and check readiness with
   `bridge.is_ready(bot_data)` — never use raw `bot_data.get("userbot_client")` directly.

4. **`_run_multi_copy` vs `_run_copy`**:
   - `_run_multi_copy` — used by fresh `/copy` launches (from `_launch_job`). Supports
     multiple destinations and correct clear-resume logic.
   - `_run_copy` — used by `/resume` and auto-resume. Single destination. Also clears
     resume on success/cancel, preserves on crash.

5. **Caption suffix (`CAPTION_SUFFIX`)** is supported by `_do_send` and `send_album` in
   `sender.py`. It must be explicitly threaded through every call site (see bug history below).

6. **DATA_DIR pattern**: All persistent files MUST use `os.getenv("DATA_DIR", fallback)` as
   the base directory, not `os.path.dirname(__file__)`. This ensures files survive redeployment
   on Railway volumes.

7. **`claim_resume()` vs `load_resume()`**: `schedule_auto_resume()` in `handlers/copybot.py`
   MUST use `_ar.claim_resume()` (NOT `_ar.load_resume()`). `claim_resume()` atomically
   renames `autoresume.json` → `autoresume.running.json` so a second process starting
   concurrently cannot also claim the same job. Using `load_resume()` caused duplicate copy
   jobs when the bot restarted more than once in quick succession (both processes would read
   the same file and launch separate copy tasks).

8. **Dedup layers** in `copy_channel_files()`:
   - **Layer 1 — Destination pre-scan**: Scans actual destination channel by `(filename, filesize)`
     into `dest_file_keys`. Runs ONCE at job start. Prevents re-sending files already there.
   - **Layer 2 — SQLite dedup**: `load_copied_ids()` returns source msg IDs + doc IDs from DB.
     Persists across restarts and redeploys.
   - **Layer 3 — Checkpoint watermark**: `last_msg_id` in checkpoint means all source msgs up
     to this ID were already processed — they are skipped via `min_id=last_msg_id` in iter_messages.

9. **`/restart` command** (new): Owner-only. Cancels active jobs, disconnects Telethon cleanly,
   then calls `os.execv(sys.executable, sys.argv)` for in-process restart. Uses `os.execv`
   (not `sys.exit`) because Railway's `restartPolicyType: ON_FAILURE` only restarts on non-zero
   exit; `os.execv` replaces the process image without exiting.

---

## Bugs Fixed — Chronological Log

### Session 1 (prior to 2026-06-25)

| Commit | File | Bug |
|---|---|---|
| `40f892b` | `bot.py` | Railway healthcheck failure — `post_shutdown` indentation error |
| `a0a2680` | `autoresume.py` | Stale-channel guard cancelled instead of updating src/dst to current config |
| `40928ae` | `autoresume.py` | Did not use `DATA_DIR` env var — persistent file was written to wrong path on Railway volume |

### Session 2 (2026-06-25) — Deep dry-run audit + fixes

| Commit | File | Bug |
|---|---|---|
| `e98e67d` | `userbot/filter_utils.py` | `_CUSTOM_PATTERNS_FILE` hardcoded relative path — ignored `DATA_DIR`; custom strip patterns were lost on Railway redeploy |
| `d3dae9d` | `handlers/purgedups.py` | Used raw `bot_data.get("userbot_client")` and `bot_data.get("userbot_ready")` instead of `bridge.get_client()` / `bridge.is_ready()` |
| `419744f` | `userbot/sync.py` | `start_sync_handler()` had no `caption_suffix` parameter — suffix watermark was silently dropped from every synced message |
| `dc6b4d4` | `handlers/copybot.py` | `_run_sync` didn't pass `caption_suffix` from opts to `start_sync_handler()` |
| `5b72afef` | `handlers/history.py` | `_do_history_copy` read `config.CAPTION_REPLACE` but never read `config.CAPTION_SUFFIX` — suffix missing from all history copies |

### Session 3 (2026-06-25) — /restart command + duplicate copy fix

| Commit | File | Change |
|---|---|---|
| `8ebde8e` | `handlers/restart.py` | **New file**: `/restart` command — owner-only graceful restart via `os.execv()` |
| `6c327ba` | `bot.py` | Register `/restart` handler + add to BotCommand list |
| `b541a74` | `handlers/autoresume.py` | Add `claim_resume()` with atomic OS rename + stale-orphan recovery; update `clear_resume()` to also remove `.running.json` |
| `dc8c646` | `handlers/copybot.py` | Use `_ar.claim_resume()` instead of `_ar.load_resume()` in `schedule_auto_resume()` — **root fix for duplicate copy files in destination** |

---

## Known Remaining Issues / Future Work

### Medium priority

1. **`purgedups.py` — no cancellation / no background task**
   The `purgedups_cmd` handler runs the full scan and delete loop inline (as `await` calls
   inside the handler coroutine). For huge channels (830K messages), this will hold the handler
   for a very long time. Should be refactored to run as `asyncio.create_task` stored in
   `bot_data["_purge_task"]` so it can be cancelled via a `/stoppurge` command.

2. **`_build_status_text` — destructive pop on read**
   `bot_data.pop("session_lost_during_copy", False)` inside `_build_status_text` means the
   "session lost" warning only ever shows once (the pop removes it). If this is intentional,
   add a comment. If not, change to `.get()`.

3. **`config.py` — hardcoded channel defaults**
   `SOURCE_CHANNEL = _int_env("SOURCE_CHANNEL", -1001811670072)` has the user's actual channel
   hardcoded as the default. If the env var is accidentally unset, it silently uses the old
   channel. Consider removing the hardcoded defaults.

### Low priority / paused

4. **Dual-userbot parallel copy feature** — explicitly paused by user. Do not implement
   without explicit re-authorization.

---

## How to Continue Work

1. All code lives in the `telegram-bot/` subdirectory. There is **no `__init__.py`** — use
   absolute imports everywhere (e.g. `import config`, not `from . import config`).

2. The GitHub token is in the `GITHUB_PERSONAL_ACCESS_TOKEN` Replit secret.
   Push changes via the GitHub Contents API (GET sha → patch content → PUT).
   **For large files** (>100KB payload), write the JSON to a temp file and use
   `curl --data @/tmp/payload.json` instead of `-d "..."` to avoid "Argument list too long".

3. To add a new persistent file, always use:
   ```python
   DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
   MY_FILE  = os.path.join(DATA_DIR, "myfile.json")
   ```

4. Always use `bridge.is_ready(bot_data)` and `bridge.get_client(bot_data)` — never
   access `bot_data` keys directly for userbot state.

5. After any change to caption processing, verify that `caption_suffix` is being passed
   through every call chain: `_do_send()`, `send_album()`, `start_sync_handler()`,
   `_do_history_copy()`, and `copy_channel_files()` (in `forwarder.py`).

6. **Never use `_ar.load_resume()` in `schedule_auto_resume()`** — always use
   `_ar.claim_resume()`. This is the fix for duplicate copy files.

7. When pushing code via GitHub API from Replit bash, always use `curl --data @file` for
   files larger than ~50KB — inline `-d "..."` fails with "Argument list too long".
