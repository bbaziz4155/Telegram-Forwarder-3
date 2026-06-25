# Telegram-Forwarder-3 — Agent Session Context

> **Purpose:** Complete working memory for any future AI agent continuing work on this repo.
> Last updated: 2026-06-25 (Session 5 — final)

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
- The 5-minute timeout IS the "skip" mechanism — no user action needed
- ⚠️ The "Send /stopjob to skip" text shown during pre-scan is MISLEADING — it should say to wait for the 5-min timeout

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

1. **Fix misleading pre-scan message** — change "Send /stopjob to skip" to "Pre-scan will time out automatically in 5 min and copying will begin"
2. **`/skipscan` command** — skip just the pre-scan without cancelling the whole job (requires adding a cancel flag checked inside `_run_prescan`)
3. **`/clearresume` command** — manually wipe autoresume.json without needing to change channel settings
4. **`/status` or `/checkstatus` command** — show status of all running tasks (copy job, sync, purge, auto-resume state) in one message
5. **Startup syntax check** — catch SyntaxErrors in all handler files before Railway healthcheck fails

---

## Session Timeline (all sessions)
- **Sessions 1–3**: Basic copy, auto-resume, multi-dest, dryrun, sync, speed control
- **Session 4**: Session revocation fix, remove channel override in _auto_resume_start, clear session_lost flag on reconnect, /stoppurge in BotCommand menu, restart_pending.json notification flow
- **Session 5 (2026-06-25)**: Fixed 5 bugs — see above. Bot is now fully operational on Railway. GitHub token: `GITHUB_PERSONAL_ACCESS_TOKEN` Replit secret (fine-grained, Contents R+W on this repo).
