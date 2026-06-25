# Telegram-Forwarder-3 — Agent Session Context

> **Purpose:** Complete working memory for any future AI agent continuing work on this repo.
> Last updated: 2026-06-25 (Session 5)

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
│   ├── copybot.py        # All copy/dryrun/sync/resume/speed/status commands (~2890 lines)
│   │                     # schedule_auto_resume() uses claim_resume() (NOT load_resume())
│   │                     # _auto_resume_start() does NOT override stored channels (see AD#7)
│   ├── menu.py           # Main menu keyboard + /commands /help text
│   ├── login.py          # /login OTP flow
│   ├── restart.py        # /restart — owner-only graceful process restart via os.execv()
│   ├── setchannel.py     # /setsource /setdest — clears autoresume on channel change
│   ├── purgedups.py      # /purgedups — dedup scan+delete (background task, /stoppurge)
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
| `CAPTION_REPLACE` | @username to replace in captions (default `""` — empty string) |
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

3. **Bridge pattern**: The userbot Telethon client lives in `bot_data["userbot_client"]`.
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

7. **Auto-resume channel integrity — DO NOT add a channel-sync guard to `schedule_auto_resume`
   or `_auto_resume_start`**. The channels stored in `autoresume.json` are written by
   `_launch_job()` at job start and are always correct. When the user changes channels via
   `/setsource` or `/setdest`, `setchannel.py` explicitly calls `_ar.clear_resume()`.
   `channel_settings.py` guarantees `config.SOURCE_CHANNEL` == last `/setsource` value, so
   reading the config values in auto-resume is both redundant and dangerous if the Railway
   env var still holds an old channel ID.

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

11. **`purgedups.py` — background task + cooperative cancellation**:
    `/purgedups` runs via `asyncio.create_task(_run_purgedups(...))` stored in
    `bot_data["active_purge_task"]`. The task checks `bot_data["_purge_cancel"]` flag every
    1000-message scan batch and every 100-message delete batch. `/stoppurge` sets the flag and
    cancels the task. `post_shutdown` in `bot.py` also cancels `"active_purge_task"` during
    Railway redeploys so no orphaned scans linger.

12. **`bot.py` `post_shutdown` task keys** — the canonical key list that gets cancelled on
    shutdown is: `("active_copy_task", "active_sync_task", "active_cleancaptions_task", "active_purge_task")`.
    The old key `"active_clean_task"` (no longer used) was a bug. If you add a new background
    task, store it under one of these keys or add a new key here.

13. **Session revocation flow**:
    - Mid-copy: health-check loop sets `bot_data["session_lost_during_copy"] = True`, cancels
      the copy task, writes `session_revoked.flag`, alerts owner.
    - On reconnect with a new session: `_clear_revoked_flag()` deletes the flag file, and
      `bot_data.pop("session_lost_during_copy", None)` clears the warning — so `/status` stops
      showing the warning once the user has fixed the session.

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
| `308bc66` | `handlers/copybot.py` | **Remove channel-sync guard** that read `config.SOURCE_CHANNEL` and overwrote correct autoresume channels |

### Session 4 (2026-06-25) — Full codebase audit (all remaining files read), 9 more bugs fixed

**Round A — fixes applied from previous session transcript:**

| Commit | File | Bug |
|---|---|---|
| (prior) | `userbot/forwarder.py` | Entity resolution failure silently returned — now raises so error surfaces to user |
| (prior) | `handlers/copybot.py` | `_run_multi_copy` exception handler couldn't edit the frozen "⏳ Initializing…" message (used wrong variable); now edits the status message with the actual error text |
| (prior) | `handlers/copybot.py` | `_build_status_text` used `.pop()` for `session_lost_during_copy` — warning vanished after first `/status`. Changed to `.get()` so warning persists |
| (prior) | `config.py` | `CAPTION_REPLACE` default was hardcoded `"@BackupChannel5211"` — changed to `""` |
| (prior) | `handlers/purgedups.py` | Entire file rewritten: scan+delete loop now runs via `asyncio.create_task(_run_purgedups(...))` so bot stays responsive; added `/stoppurge` cancel command with cooperative flag-based cancellation checked every 1000-scan / 100-delete batch |

**Round B — new bugs found and fixed in this session:**

| Commit | File | Bug |
|---|---|---|
| `608cdfa` | `bot.py` | `post_shutdown` cancelled `"active_clean_task"` (non-existent key) instead of `"active_cleancaptions_task"`; also added `"active_purge_task"` — both tasks were never cancelled on Railway redeploy |
| `608cdfa` | `bot.py` | `/stoppurge` registered as handler but missing from `set_my_commands()` — users couldn't discover it in the Telegram `"/"` command menu |
| `37903ab` | `handlers/copybot.py` | `_auto_resume_start` overrode stored `src`/`dst` from `autoresume.json` with `config.SOURCE_CHANNEL` / `config.DEST_CHANNEL`, contradicting the autoresume safety contract (AD#7); removed the override block |
| `b41356b` | `handlers/menu.py` | `/stoppurge` missing from the `commands_cmd()` Maintenance section — users reading `/commands` couldn't find the cancel command |
| `fccd7c1` | `userbot_bridge.py` | `session_lost_during_copy` flag set on revocation but never cleared on reconnect — after the `pop→get` fix it would persist forever; now cleared alongside `_revocation_alerted` when a new session connects successfully |

---

## Known Remaining Issues / Future Work

### Low priority

1. **`_run_copy` (auto-resume path) — confusing done message on entity error**
   If `copy_channel_files()` raises an exception in the auto-resume path, `_run_copy` calls
   `notifier.done(0, 0, 0, 0)` which shows "⚠️ Copy Finished (with errors)" in the status
   message, then sends a separate `❌ Copy error: …` message. The status message is misleading
   (shows 0/0/0 stats for what is really an error). Low impact since `notifier.done()` is
   immediately followed by an explicit error `send_message`. Fix: call
   `notifier.edit_progress("❌ Error: …")` instead of `notifier.done()`.

2. **Dual-userbot parallel copy feature** — explicitly paused by user. Do not implement
   without explicit re-authorization.

---

## How to Continue Work

1. All code lives in the `telegram-bot/` subdirectory. No `__init__.py` — use absolute
   imports everywhere.

2. The GitHub token is in the `GITHUB_PERSONAL_ACCESS_TOKEN` Replit secret.
   Push via GitHub Contents API (GET sha → patch content → PUT).
   **For large files** (>~50KB, e.g. `copybot.py`), the inline base64 shell approach hits
   "Argument list too long". Use Python's `urllib.request` instead:
   ```python
   import json, base64, urllib.request, os
   with open('/tmp/fixed.py', 'rb') as f:
       content_b64 = base64.b64encode(f.read()).decode()
   payload = {"message": "...", "content": content_b64, "sha": "<current_sha>"}
   req = urllib.request.Request(
       "https://api.github.com/repos/bbaziz4155/Telegram-Forwarder-3/contents/telegram-bot/handlers/copybot.py",
       data=json.dumps(payload).encode(), method="PUT",
       headers={"Authorization": f"token {os.environ['GITHUB_PERSONAL_ACCESS_TOKEN']}",
                "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"})
   result = json.loads(urllib.request.urlopen(req).read())
   print(result['commit']['sha'])
   ```

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
   `schedule_auto_resume()` or `_auto_resume_start()`. The autoresume.json channels are
   always correct. Channel changes clear the resume via `setchannel.py`.

8. **When adding a new long-running background task**, store it in `bot_data` under a key
   ending in `_task`, add cooperative cancellation (check a cancel flag every N iterations),
   add a `/stop<name>` command, register it in `bot.py`'s `set_my_commands()` and in
   `menu.py`'s `commands_cmd()`, and add its key to the `post_shutdown` cancellation loop.

---

## Session 5 — Bug Fixes (2026-06-25)

### Bugs Fixed

**Bug 1 — CRITICAL SyntaxError (commit d88de090)**
- File: `handlers/copybot.py`, function `_run_multi_copy`
- Was: Two consecutive `except Exception as e:` lines → Python `SyntaxError` at import time
  → bot crashed on every startup → all Railway deployments failing with healthcheck timeout
- Fix: Removed the duplicate line

**Bug 2 — Misleading "Copy Finished" on crash (commit d88de090)**
- File: `handlers/copybot.py`, function `_run_copy`
- Was: `except Exception` block called `notifier.done()` which showed
  "⚠️ Copy Finished (with errors)" even when the job crashed mid-run (not finished at all)
  then sent a separate "❌ Copy error: …" message
- Fix: Replaced `notifier.done()` call with `bot.edit_message_text()` that shows the
  actual error message and a hint to use `/resume`

**Bug 3 — /stoppurge broken during delete phase (commit 899feaee)**
- File: `handlers/purgedups.py`, function `_run_purgedups`
- Was: `active_purge_task` and `_PURGE_CANCEL_KEY` were cleared in a `finally` block
  attached to the scan-phase `try`, which ran as soon as scanning finished — BEFORE
  the delete phase started. `/stoppurge` showed "no job running" during deletion.
- Fix: Removed the scan-phase `finally`; added a `_cleanup()` helper (clears both
  `active_purge_task` and `_PURGE_CANCEL_KEY`) called explicitly at every exit path:
  cancel-inside-scan-loop, CancelledError, Exception, no-dups path, cancel-during-delete,
  and normal completion.

### State After Session 5
- ✅ Bot starts and deploys successfully on Railway
- ✅ No known remaining bugs
- The GitHub token used for pushes: `GITHUB_PERSONAL_ACCESS_TOKEN` Replit secret
  (Fine-grained, Contents: Read+Write on this repo)
