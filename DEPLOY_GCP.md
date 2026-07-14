# Hosting this bot for free on Google Cloud

This bot needs to run 24/7 and keep a small local database (so it
remembers what it already copied). Google Cloud's "Always Free" tier
includes one small server (e2-micro) that never expires and never
sleeps, which is a good fit — unlike free web-app hosts, nothing gets
wiped between restarts.

Google requires a credit card on file for identity verification, but
the Always Free resources below cost nothing as long as you stay
within the listed limits.

## 1. Create the free account & VM

1. Go to https://cloud.google.com/free and sign up (adds your card for
   verification only).
2. In the console, open **Compute Engine → VM instances → Create
   Instance**.
3. Set:
   - **Region**: one of `us-west1`, `us-central1`, or `us-east1`
     (required for the free tier).
   - **Machine type**: `e2-micro`.
   - **Boot disk**: Ubuntu 22.04 LTS, standard persistent disk, 30 GB
     or less (30 GB standard disk is included free).
4. Leave the rest as default and click **Create**. Wait for it to
   start, then click **SSH** next to the instance to open a terminal
   in your browser — no extra setup needed.

## 2. Install Docker

Paste this into the SSH terminal:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

## 3. Get the bot's code onto the server

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/bbaziz4155/Telegram-Forwarder-3.git
cd Telegram-Forwarder-3
```

## 4. Configure your secrets

```bash
cp telegram-bot/.env.example telegram-bot/.env
nano telegram-bot/.env
```

Fill in at minimum `BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`.
Leave `SESSION_STRING` blank for now — you'll generate it in step 6.
Save with `Ctrl+O`, then `Ctrl+X` to exit.

## 5. Start the bot

```bash
docker compose up -d --build
docker compose logs -f
```

You should see `Starting Telegram Forwarder Bot...` in the logs.
Press `Ctrl+C` to stop watching logs (the bot keeps running in the
background).

## 6. Log the userbot in (one-time)

Message your bot on Telegram: `/gensession`. Follow the prompts (phone
number, the code Telegram sends you, and your 2FA password if you have
one). It replies with a long string — copy it.

Back in the SSH terminal:

```bash
nano telegram-bot/.env
```

Paste the string as `SESSION_STRING=...`, save, then restart:

```bash
docker compose up -d --build
```

## 7. Confirm it survives a reboot

```bash
sudo reboot
```

Wait ~30 seconds, reconnect via SSH, and run `docker compose ps` — the
bot container should already be back up (Docker's `restart:
unless-stopped` policy handles this automatically).

## Updating later

Whenever you push changes to GitHub:

```bash
cd Telegram-Forwarder-3
git pull
docker compose up -d --build
```

## Useful commands

| Command | What it does |
|---|---|
| `docker compose logs -f` | Watch live logs |
| `docker compose restart` | Restart the bot |
| `docker compose down` | Stop the bot |
| `docker compose up -d --build` | Start/rebuild after code or `.env` changes |

Your copy history and settings live in the `data/` folder next to
`docker-compose.yml` on the VM — back it up occasionally if you want
extra peace of mind, but it isn't required for the bot to keep working.
