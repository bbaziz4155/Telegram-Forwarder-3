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
│   ├── copybot.py        # All copy/dryrun/sync/resume/speed/status commands (~2880 lines)
│   │                     # schedule_auto_resume() uses claim_resume() (NOT load_resume())
│   │                     # NO channel-sync guard — see Architecture Decision #7
│   ├── menu.py           # Main menu keyboard
│   ├── login.py          # /login OTP flow
│   ├── restart.py        # /restart — owner-only graceful process restart via os.execv()
│   ├── setchannel.py     # /setsource /setdest — clears autoresume on channel change
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
| `SOURCE_CHANNEL` | **Default** source channel ID (int) — only used as initial fallback if channel_settings.json doesn't exist. /setsource overrides it. |
| `DEST_CHANNEL` | **Default** destination channel ID (int) — only used as initial fallback. /setdest overrides it. |
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
   any `input()` call from blocking the async event loop.

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
   `sender.py`. It must be explicitly threaded through every call site.

6. **DATA_DIR pattern**: All persistent files MUST use `os.getenv("DATA_DIR", fallback)` as
   the base directory, not `os.path.dirname(__file__)`. This ensures files survive redeployment
   on Railway volumes.

7. **Auto-resume channel integrity — DO NOT add a channel-sync guard to `schedule_auto_resume`**.
   The channels stored in `autoresume.json` are written by `_launch_job()` at job start and
   are always correct. When the user changes channels via `/setsource` or `/setdest`,
   `setchannel.py` explicitly calls `_ar.clear_resume()` which wipes the resume state.
   A channel-sync guard that reads `config.SOURCE_CHANNEL` will corrupt the resume when
   the `SOURCE_CHANNEL` env var (Railway) is still set to an old channel. This was a real
   bug that caused the bot to always copy from the old source after restart.

8. **`claim_resume()` vs `load_resume()`**:
   - `schedule_auto_resume()` in `handlers/copybot.py` MUST use `_ar.claim_resume()`.
     `claim_resume()` atomically renames `autoresume.json` → `autoresume.running.json` so a
     second process starting concurrently cannot also claim the same job (prevents duplicate
     copies on rapid double-restart).
   - `load_resume()` is for inspect-only reads. It checks BOTH `autoresume.json` AND
     `autoresume.running.json` (fallback) so `/setsource`/`/setdest` can detect a claimed
     resume and clear it.

9. **Dedup layers** in `copy_channel_files()`:
   - **Layer 1 — Destination pre-scan**: Scans actual destination channel by `(filename, filesize)`
     into `dest_file_keys`. Runs ONCE at job start.
   - **Layer 2 — SQLite dedup**: `load_copied_ids()` returns source msg IDs + doc IDs from DB.
     Persists across restarts and redeploys.
   - **Layer 3 — Checkpoint watermark**: `last_msg_id` in checkpoint — all source msgs up
     to this ID are skipped via `min_id=last_msg_id` in `iter_messages`.

10. **`/restart` command**: Owner-only. Cancels active jobs, disconnects Telethon cleanly,
    then calls `os.execv(sys.executable, sys.argv)` for in-process restart. Uses `os.execv`
    (not `sys.exit`) because Railway's `restartPolicyType: ON_FAILURE` only restarts on
    non-zero exit; `os.execv` replaces the process image without exiting.

---

## Bugs Fixed — Chronological Log

### Session 1 (prior to 2026-06-25)

| Commit | File | Bug |
|---|---|---|
| `40f892b` | `bot.py` | Railway healthcheck failure — `post_shutdown` indentation error |
| `a0a2680` | `autoresume.py` | Stale-channel guard cancelled instead of updating src/dst |
| `40928ae` | `autoresume.py` | Did not use `DATA_DIR` env var — file written to wrong path |

### Session 2 (2026-06-25) — Deep dry-run audit + fixes

| Commit | File | Bug |
|---|---|---|
| `e98e67d` | `userbot/filter_utils.py` | `_CUSTOM_PATTERNS_FILE` ignored `DATA_DIR`; patterns lost on redeploy |
| `d3dae9d` | `handlers/purgedups.py` | Used raw `bot_data.get()` instead of bridge API |
| `419744f` | `userbot/sync.py` | `start_sync_handler()` had no `caption_suffix` parameter |
| `dc6b4d4` | `handlers/copybot.py` | `_run_sync` didn't pass `caption_suffix` to `start_sync_handler()` |
| `5b72afef` | `handlers/history.py` | `_do_history_copy` never read `config.CAPTION_SUFFIX` |

### Session 3 (2026-06-25) — /restart command + duplicate copy fix + stale-channel fix

| Commit | File | Change |
|---|---|---|
| `8ebde8e` | `handlers/restart.py` | **New**: `/restart` owner-only graceful restart via `os.execv()` |
| `6c327ba` | `bot.py` | Register `/restart` handler + add to BotCommand list |
| `b541a74` | `handlers/autoresume.py` | Add `claim_resume()` with atomic OS rename + stale-orphan recovery; `clear_resume()` removes both files |
| `dc8c646` | `handlers/copybot.py` | Use `_ar.claim_resume()` in `schedule_auto_resume()` — prevents duplicate copy files |
| `30a9640` | `handlers/autoresume.py` | `load_resume()` also checks `.running.json` so `/setsource` can detect claimed resume |
| `308bc66` | `handlers/copybot.py` | **Remove channel-sync guard** that read `config.SOURCE_CHANNEL` (stale env var) and overwrote correct autoresume channels — was causing bot to always copy from old source after restart |

---

## Known Remaining Issues / Future Work

### Medium priority

1. **`purgedups.py` — no cancellation / no background task**
   Runs the full scan and delete loop inline. For 830K-message channels, holds the handler
   for a very long time. Should be refactored to `asyncio.create_task` stored in
   `bot_data["_purge_task"]` cancellable via `/stoppurge`.

2. **`_build_status_text` — destructive pop on read**
   `bot_data.pop("session_lost_during_copy", False)` inside `_build_status_text` means the
   "session lost" warning only shows once. If unintentional, change to `.get()`.

3. **`config.py` — hardcoded channel default**
   `SOURCE_CHANNEL = _int_env("SOURCE_CHANNEL", -1001811670072)` has the user's old channel
   hardcoded. If `SOURCE_CHANNEL` env var is unset, it silently uses this stale default.
   Consider removing the hardcoded default (set to `None`).

### Low priority / paused

4. **Dual-userbot parallel copy feature** — explicitly paused by user. Do not implement
   without explicit re-authorization.

---

## How to Continue Work

1. All code lives in the `telegram-bot/` subdirectory. No `__init__.py` — use absolute
   imports everywhere.

2. The GitHub token is in the `GITHUB_PERSONAL_ACCESS_TOKEN` Replit secret.
   Push via GitHub Contents API (GET sha → patch content → PUT).
   **For large files** (>50KB payload), write JSON to a temp file and use
   `curl --data @/tmp/payload.json` to avoid "Argument list too long".

3. To add a new persistent file, always use:
   ```python
   DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
   MY_FILE  = os.path.join(DATA_DIR, "myfile.json")
   ```

4. Always use `bridge.is_ready(bot_data)` and `bridge.get_client(bot_data)` — never
   access `bot_data` keys directly for userbot state.

5. After any change to caption processing, verify `caption_suffix` passes through every
   call chain: `_do_send()`, `send_album()`, `start_sync_handler()`, `_do_history_copy()`,
   and `copy_channel_files()`.

6. **Never use `_ar.load_resume()` in `schedule_auto_resume()`** — always use
   `_ar.claim_resume()`. This prevents duplicate copy jobs on rapid double-restart.

7. **Never add a channel-sync guard** that reads `config.SOURCE_CHANNEL` in
   `schedule_auto_resume()`. The autoresume.json channels are always correct (set by
   `/copy`). Channel changes clear the resume via `setchannel.py`. A guard corrupts the
   resume when the Railway env var still has the old channel ID.
