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
├── autoresume.py         # Load/save/clear resume state — uses DATA_DIR
│
├── handlers/
│   ├── copybot.py        # All copy/dryrun/sync/resume/speed/status commands (~2900 lines)
│   ├── autoresume.py     # Auto-resume-on-boot logic
│   ├── menu.py           # Main menu keyboard
│   ├── login.py          # /login OTP flow
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
