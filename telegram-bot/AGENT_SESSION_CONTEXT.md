# Telegram-Forwarder-3 — Agent Session Context

> **Purpose:** Complete working memory for any future AI agent continuing work on this repo.
> Last updated: 2026-06-25 (Session 6 — final)

---

## Project Summary

A Telegram **userbot + bot hybrid** that bulk-copies ~800K messages from a source channel
to a destination channel without "Forwarded from" tags. Hosted on **Railway**.

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
├── main.py               # Entry point — health HTTP server FIRST, then bot
├── bot.py                # PTB app setup, all handler registration, post_init, post_shutdown
├── config.py             # Env var defaults (SOURCE_CHANNEL, DEST_CHANNEL, etc.)
├── database.py           # aiosqlite — forward_rules, ignore_list, copied_files, admins
├── states.py             # ConversationHandler state constants
├── userbot_bridge.py     # Bridge API — always use bridge.get_client() / bridge.is_ready()
├── channel_settings.py   # Persists /setsource + /setdest to DATA_DIR/channel_settings.json
│
├── handlers/
│   ├── copybot.py        # ALL copy/dryrun/sync/resume/speed/status cmds (~2930 lines)
│   ├── autoresume.py     # save_resume(), clear_resume(), claim_resume(), load_resume()
│   ├── purgedups.py      # /purgedups + /stoppurge
│   ├── menu.py           # Main menu keyboard + /commands /help
│   ├── login.py          # /login OTP flow
│   ├── restart.py        # /restart — graceful process restart
│   ├── setchannel.py     # /setsource /setdest /channels — clears autoresume on channel change
│   ├── strippatterns.py  # /strippatterns conversation
│   ├── channelinfo.py    # /channelinfo
│   ├── admin_mgmt.py     # Admin allow/deny
│   ├── history.py        # /forwardhistory
│   ├── rules.py          # Auto-forward rules (DB-backed)
│   └── ignore.py         # Ignore-list (DB-backed)
│
└── userbot/
    ├── forwarder.py       # copy_channel_files() — the bulk-copy engine + pre-scan
    ├── sender.py          # _do_send(), send_album()
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
| `SOURCE_CHANNEL` | Default source channel ID — overridden by /setsource (channel_settings.json) |
| `DEST_CHANNEL` | Default destination channel ID — overridden by /setdest |
| `DATA_DIR` | **Critical** — path for all persistent files. Must be a Railway Volume mount. |
| `DATABASE_URL` | PostgreSQL connection string |
| `CAPTION_REPLACE` | @username to replace in captions |
| `CAPTION_SUFFIX` | Watermark text appended to every caption |
| `STRIP_PATTERNS` | JSON list of regex patterns to strip from captions |
| `ALLOWED_EXTS` | Comma-separated file extensions (blank = all) |
| `SKIP_TEXT` | "true" = skip text-only messages |
| `NOTIFY_EVERY` | Progress notification interval (0 = off) |
| `ADMIN_IDS` | Comma-separated Telegram user IDs allowed to use the bot |

---

## Architecture Decisions & Rules (NEVER break these)

**AD1 — Bridge pattern**
Always access the Telethon client via `bridge.get_client(bot_data)` and
`bridge.is_ready(bot_data)`. Never use raw `bot_data.get("userbot_client")` directly.

**AD2 — Health server order**
`main.py` starts the HTTP healthcheck server BEFORE starting the bot.
Railway expects a fast 200 OK — never reorder or delay this.

**AD3 — Auto-resume lifecycle**
- `save_resume()` called in `_launch_job()` the moment a copy task is created
- `clear_resume()` called in `_run_copy()` finally — on normal finish OR user /stopjob
- `claim_resume()` used at startup in `schedule_auto_resume` (NOT `load_resume()`)
- `/setsource` and `/setdest` both call `clear_resume()` if a pending resume exists
- `_auto_resume_start()` does NOT override stored channels — uses src/dst from autoresume.json

**AD4 — Channel mismatch guard**
Added in Session 5. In `schedule_auto_resume`, after claiming the resume, the code
compares stored src/dst with `config.SOURCE_CHANNEL` / `config.DEST_CHANNEL`. If they
differ, the resume is cleared and the user is notified. This prevents the bot from
silently resuming old jobs with stale channel IDs after the user ran /setsource.

**AD5 — DATA_DIR everywhere**
All persistent files (checkpoints, autoresume.json, channel_settings.json, strip_patterns.json,
restart_pending.json) use the `DATA_DIR` env var. Never hardcode paths.

**AD6 — post_shutdown disconnects Telethon**
This must always run or the session gets revoked by Telegram. Never remove it.

**AD7 — interactive=False**
Always passed to `copy_channel_files()` — prevents `input()` from blocking the async loop.

**AD8 — No bare newlines in string literals**
Python SyntaxError if a regular `"..."` string contains a literal newline character.
Always use `\n` escape sequences. This is how Bug 4 happened.

---

## Pre-scan Behavior (important for large channels)

On every `/copy`, `forwarder.py` pre-scans the **destination** channel to build a set of
already-copied (filename, filesize) pairs. This prevents re-sending files that were copied
in a previous run when Railway wiped the SQLite DB.

- `_PRESCAN_TIMEOUT = 300` seconds — hard 5-minute limit; copying starts regardless after this
- Telethon scans ~300–500 messages/second during pre-scan
- `/stopjob` during pre-scan cancels the ENTIRE job (not just the scan)
- `/skipscan` during pre-scan fires a skip-event that exits the scan immediately and starts copying with whatever was found so far
- The 5-minute timeout auto-exits the scan even without `/skipscan`
- The "Send /stopjob to skip" text is gone — replaced with a correct timeout notice

## User's Scale (current as of 2026-06-25)
- Source channel: ~800,000 files (8 lakh), ID: `-1001957754060`
- Destination channel: ID: `-1003563437550`
- Old (stale) source channel that kept appearing: `-1001811670072` — fixed by AD4
- Copy rate: ~1 file/1–3 seconds → ~18–19 days to copy all 800K files uninterrupted
- Railway restarts the process; auto-resume picks it back up from checkpoint

---

## Session 5 — All Bugs Fixed (2026-06-25)

**Bug 1 — CRITICAL SyntaxError (commit d88de090)**
- File: `handlers/copybot.py`, function `_run_multi_copy`
- Was: Two consecutive `except Exception as e:` lines → Python SyntaxError → bot crashed on every startup → ALL Railway deployments failing
- Fix: Removed the duplicate line

**Bug 2 — Misleading "Copy Finished" on crash (commit d88de090)**
- File: `handlers/copybot.py`, function `_run_copy`
- Was: `except Exception` block called `notifier.done()` → showed "⚠️ Copy Finished (with errors)" even on hard crash (job never finished)
- Fix: Replaced with `bot.edit_message_text()` showing the actual error and a hint to use /resume

**Bug 3 — /stoppurge broken during delete phase (commit 899feaee)**
- File: `handlers/purgedups.py`, function `_run_purgedups`
- Was: `active_purge_task` + `_PURGE_CANCEL_KEY` cleared in scan-phase `finally` block → that block ran as soon as scanning finished, before the delete phase → /stoppurge said "no job running" during deletion
- Fix: Added `_cleanup()` helper called explicitly at every exit path (cancel-in-scan, CancelledError, Exception, no-dups, cancel-in-delete, normal completion)

**Bug 4 — SyntaxError in bot.py restart notification (commit 09c2e5c3)**
- File: `bot.py`, function `post_init`
- Was: Literal newline characters inside regular `"..."` string literals in the `send_message()` call → Python SyntaxError → bot crashed on startup even after Bug 1 was fixed
- Fix: Replaced literal newlines with `\n` escape sequences
- Root cause: Session 4 commit wrote the strings with actual newlines instead of \n

**Bug 5 — Auto-resume silently uses stale channel IDs (commit f2d9743a)**
- File: `handlers/copybot.py`, function `schedule_auto_resume`
- Was: After restart, auto-resume launched jobs using src/dst from autoresume.json without checking if the user had changed channels since then — kept using old source `-1001811670072`
- Fix: Channel mismatch guard (AD4) — if stored channels ≠ current config, clear the resume file and send user a detailed cancellation notice

---

## Session 6 — Items Fixed (2026-06-25)

**Item 1 — Misleading pre-scan message (copybot.py + forwarder.py)**
- Was: "Send /stopjob to skip and start immediately." — misleading because /stopjob kills the whole job
- Fix: Message now reads "Pre-scan times out automatically in 5 min — copying will start on its own." (copybot.py)
  + "Pre-scan times out automatically after 5 min — copying will start on its own." (forwarder.py info log)

**Item 2 — `/skipscan` command (copybot.py + forwarder.py)**
- Was: No way to skip just the pre-scan; /stopjob cancelled the whole job
- Fix: `asyncio.Event()` created per job in `_launch_job()` → stored in `bot_data["prescan_skip_event"]`
  Passed via `prescan_skip_event=` kwarg to all 4 `copy_channel_files()` call sites
  `forwarder.py` checks `event.is_set()` inside the `_run_prescan()` loop and exits early
  `/skipscan` command sets the event; registered in `get_extra_handlers()` and `bot.py` BotCommand menu

**Item 3 — `/clearresume` command (copybot.py)**
- Was: Only way to clear autoresume was to change channels via /setsource or /setdest
- Fix: `clearresume_cmd` — blocks if a job is running, otherwise calls `_ar.clear_resume()` and confirms
  Registered in `get_extra_handlers()` and `bot.py` BotCommand menu

**Item 4 — Purge job missing from `/status` (copybot.py)**
- Was: `_build_status_text()` showed copy job + sync but not purge job
- Fix: Added check for `bot_data["active_purge_task"]` at end of `_build_status_text()`

**Item 5 — Startup syntax check (main.py)**
- Was: A SyntaxError in any handler file caused Railway to show a cryptic traceback and kill the healthcheck
- Fix: After health server starts (so Railway gets a fast 200 OK), `ast.parse()` all `*.py` files recursively
  On failure: logs a clear error with file + line + message, then blocks (keeps health server alive) without starting the bot

---

## How to Push Fixes to GitHub from Replit

```bash
# Token is in GITHUB_PERSONAL_ACCESS_TOKEN Replit secret (available in bash env)
# Always use the GitHub Contents API — not git push

# Step 1: Get current SHA of a file
curl -s -H "Authorization: Bearer $GITHUB_PERSONAL_ACCESS_TOKEN" \
  "https://api.github.com/repos/bbaziz4155/Telegram-Forwarder-3/contents/telegram-bot/handlers/FILE.py" | \
  node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>{const j=JSON.parse(d);console.log('SHA:',j.sha);require('fs').writeFileSync('/tmp/file.py',Buffer.from(j.content.replace(/\n/g,''),'base64').toString('utf8'));});"

# Step 2: Edit /tmp/file.py with Node.js line splicing

# Step 3: Push
node -e "
import https from 'https';
const data = JSON.stringify({message:'fix: ...',content:Buffer.from(require('fs').readFileSync('/tmp/file.py')).toString('base64'),sha:'<SHA>'});
const req = https.request({hostname:'api.github.com',path:'/repos/bbaziz4155/Telegram-Forwarder-3/contents/telegram-bot/handlers/FILE.py',method:'PUT',headers:{'Authorization':'Bearer '+process.env.GITHUB_PERSONAL_ACCESS_TOKEN,'Content-Type':'application/json','Content-Length':Buffer.byteLength(data),'User-Agent':'replit-agent','Accept':'application/vnd.github+json'}},(res)=>{let d='';res.on('data',c=>d+=c);res.on('end',()=>console.log(res.statusCode,JSON.parse(d).content?.sha));});req.write(data);req.end();
"
```

---

## Remaining Known Items / Suggested Next Features

All 5 items from Session 5 are resolved as of Session 6. Bot is fully operational.

**Possible future work:**
- Progress ETA estimate in `/status` (files remaining ÷ current rate)
- Retry backoff for Telegram flood-wait errors (currently just waits the specified time)
- Admin-only broadcast command to notify all users of maintenance restarts

---

## Session Timeline (all sessions)
- **Sessions 1–3**: Basic copy, auto-resume, multi-dest, dryrun, sync, speed control
- **Session 4**: Session revocation fix, remove channel override in _auto_resume_start, clear session_lost flag on reconnect, /stoppurge in BotCommand menu, restart_pending.json notification flow
- **Session 5 (2026-06-25)**: Fixed 5 bugs — see above. Bot is now fully operational on Railway. GitHub token: `GITHUB_PERSONAL_ACCESS_TOKEN` Replit secret (fine-grained, Contents R+W on this repo).
- **Session 6 (2026-06-25)**: Fixed all 4 remaining open items — misleading pre-scan message, /skipscan command, /clearresume command, purge status in /status, startup syntax check. No open items remain.
