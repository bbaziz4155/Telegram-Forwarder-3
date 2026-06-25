# Telegram Forwarder Bot — Agent Session Context
> Last updated: 2026-06-25  
> For: any agent continuing work on `bbaziz4155/Telegram-Forwarder-3`

---

## What this bot does

A Telegram **copy-bot**: copies every message from a source channel/group to one or more destination channels, without the "Forwarded from" watermark. Runs on Python + `python-telegram-bot` (PTB v20) + Telethon (userbot).

Key capabilities:
- **Copy mode** — bulk copy existing messages, resumable via checkpoint
- **Sync mode** — live-forward new incoming messages
- **Dry-run** — simulate a copy without sending (shows what would be skipped)
- **Dual-copy** *(new, this session)* — two Telegram accounts copy in parallel, halving large-channel copy time
- Auto-resume after bot restart
- Per-message dedup via SQLite
- Flood-wait handling with live countdown in status message
- Caption find-replace, @username replacement, file-type filtering

---

## Repo layout

```
telegram-bot/
├── bot.py                     # PTB Application setup, startup/shutdown, command registration
├── userbot_bridge.py          # Telethon client lifecycle, bridge between PTB and Telethon
├── handlers/
│   ├── copybot.py             # All /copy logic: conversation wizard, keyboard, job runner
│   ├── syncbot.py             # /sync handler
│   ├── admin.py               # /status, /stopjob, /resume, /speed, /gensession
│   └── …
├── userbot/
│   ├── forwarder.py           # copy_channel_files() — the actual Telethon copy loop
│   ├── checkpoint.py          # SQLite checkpoint (last copied msg ID)
│   └── dedup.py              # SQLite dedup table (message IDs already sent)
├── autoresume.py              # Persist/load auto-resume state across bot restarts
└── requirements.txt
```

---

## Architecture — how a copy job works

```
User /copy
  → got_source() → got_dest() → options keyboard (ConversationHandler)
  → _launch_job()
      ├─ normal:    asyncio.create_task(_run_copy())    → stored in bot_data["active_copy_task"]
      └─ dual mode: asyncio.create_task(_run_dual_copy()) → same key

_run_copy() / _run_dual_copy()
  └─ copy_channel_files(client, src, dst, …)
       └─ client.iter_messages(source_entity, min_id=resume_from, max_id=…, reverse=True)
            └─ for each message: dedup check → client.send_message/send_file → dedup mark
```

**bot_data keys** (runtime state, all in `context.bot_data`):
| Key | Set by | Meaning |
|---|---|---|
| `active_copy_task` | `_launch_job` | asyncio.Task for current copy/dual job |
| `active_status_msg` | `_launch_job` | message_id of the live progress message |
| `active_copy_stats` | `BotProgressNotifier` | `{copied, skipped, failed, total}` for Account A |
| `active_copy_stats_b` | `BotProgressNotifier` (dual) | same for Account B |
| `active_flood_wait` | `BotProgressNotifier` | `{until: timestamp}` while flood-wait active (A) |
| `active_flood_wait_b` | `BotProgressNotifier` (dual) | same for B |
| `active_copy_delay` | `/speed` command | override rate delay in seconds |
| `userbot_client` | `userbot_bridge` | primary Telethon client (Account A) |
| `userbot_client_2` | `userbot_bridge` | secondary Telethon client (Account B, dual only) |
| `userbot_ready` | `userbot_bridge` | bool — Account A connected |
| `userbot_ready_2` | `userbot_bridge` | bool — Account B connected |

---

## Dual-copy feature (added this session)

### Overview
Optional toggle in the /copy wizard. Splits the source channel's message ID range in half, assigns each half to a separate Telethon account. Both run concurrently, sharing the same dedup table.

### Activation
- Requires `SESSION_STRING_2` env var (second Telethon account session string)
- The 🔀 button only appears in the /copy wizard when Account B is connected
- Default: OFF. User toggles per-job. Resets to OFF each new /copy

### Split logic
```python
max_msg_id = (await client.get_messages(src, limit=1))[0].id
mid_id     = max_msg_id // 2
# Worker A (Account A): messages with id >= mid_id  (newer half)
# Worker B (Account B): messages with id <  mid_id  (older half)
```
- Worker A: `copy_channel_files(client,   src, dst, min_id=mid_id)`
- Worker B: `copy_channel_files(client_2, src, dst, max_id=mid_id+1)`

### Status display (live, edits every 10s)
```
🔀 Dual Copy running
  👤 Account A: ✅1,234 / 5,000 ⏭56 ❌0
  👤 Account B: ✅987 ⏭34 ❌0 ⏳2m30s
```

### Known limitations (acceptable for MVP)
- Auto-resume after bot crash restarts as single-account (dual state not persisted)
- Checkpoint file is written by both workers concurrently (safe in asyncio single-thread); checkpoint after a dual job may be inconsistent, but dedup prevents re-sends

---

## Files changed this session (6 commits)

| Commit | File | Change |
|---|---|---|
| `0bd75856` | `userbot/forwarder.py` | Add `min_id`/`max_id` params to `copy_channel_files()` |
| `3e51382a` | `userbot_bridge.py` | Add `SESSION_STRING_2`, `_connect_loop_2`, `get_client_2`/`is_ready_2`/`is_locked_2` |
| `f28fc90f` | `bot.py` | Graceful shutdown: cancel `active_copy_task_2`, disconnect `userbot_client_2` |
| `99512cb9` | `handlers/copybot.py` | Full dual-copy: `BotProgressNotifier` stats_key/flood_key, `_opts_keyboard` dual toggle, `_run_dual_copy`, `_build_status_text` dual rows, `stopjob_cmd` dual-aware |
| `775c697c` | `userbot/forwarder.py` | **Bugfix**: `max_id=None→0` (Telethon expects int) |
| `14406636` | `handlers/copybot.py` | **Bugfix**: `got_dest` passes `dual_available`; `errored` flag in `_run_dual_copy` finally |

---

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram Bot API token |
| `SESSION_STRING` | ✅ | Telethon session string for Account A (primary userbot) |
| `SESSION_STRING_2` | ⚪ optional | Telethon session string for Account B (enables dual-copy toggle) |
| `DATABASE_URL` | ⚪ | Postgres (if used; SQLite is default for dedup/checkpoint) |

---

## Key patterns for future agents

### Adding a new /copy option
1. Add key + default to `_default_opts()` in `copybot.py`
2. Add button in `_opts_keyboard()`
3. Handle the `callback_data` in `options_callback()`
4. Pass to `copy_channel_files()` in `_run_copy()` (and `_run_dual_copy()` if relevant)

### Updating the status message
- `_build_status_text(bot_data)` in `copybot.py` builds the full /status output
- Dual-copy block: check `bot_data.get("active_copy_stats_b")` — if not None, render dual rows

### GitHub API push pattern (bash, python not available)
```bash
# Encode file and push via GitHub Contents API
node -e "
const fs=require('fs');
const body=JSON.stringify({message:'…',content:fs.readFileSync('file.py').toString('base64'),sha:'<current_sha>'});
fs.writeFileSync('/tmp/body.json',body);
" 
curl -s -X PUT \
  -H "Authorization: token $GITHUB_PERSONAL_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/body.json \
  "https://api.github.com/repos/bbaziz4155/Telegram-Forwarder-3/contents/<path>"
```
Get current SHA first:
```bash
curl -s -H "Authorization: token $GITHUB_PERSONAL_ACCESS_TOKEN" \
  "https://api.github.com/repos/bbaziz4155/Telegram-Forwarder-3/contents/<path>" | node -e "…j.sha…"
```

---

## What to work on next

- `/dualstatus` command — show each account's connection health and active flood wait
- Per-account `/speed2` — throttle Account B independently
- Persist dual-copy resume state so crash-restart continues as dual (not single-account)
- Dual-copy progress bar (estimated % based on message ID range)
