import os
import sys
import asyncio
import logging
from colorama import Fore, Style, init as colorama_init
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    ApiIdInvalidError,
)

from .menu import run_menu

colorama_init(autoreset=True)
logger = logging.getLogger(__name__)

API_ID   = int(os.environ.get("TELEGRAM_API_ID",   "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

SESSION_PATH = os.path.join(os.path.dirname(__file__), "..", "sessions", "userbot")


def _ensure_dirs():
    for sub in ("sessions", "data", "data/checkpoints"):
        path = os.path.join(os.path.dirname(__file__), "..", sub)
        os.makedirs(path, exist_ok=True)


class UserBot:
    def __init__(self):
        _ensure_dirs()
        if not API_ID or not API_HASH:
            print(Fore.RED + "❌  TELEGRAM_API_ID and TELEGRAM_API_HASH must be set." + Style.RESET_ALL)
            sys.exit(1)
        self.client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

    async def run(self):
        print(Fore.CYAN + "\n" + "="*54 + Style.RESET_ALL)
        print(Fore.CYAN + "  🤖  Telegram Userbot — Channel File Copier" + Style.RESET_ALL)
        print(Fore.CYAN + "="*54 + "\n" + Style.RESET_ALL)

        # Retry on "database is locked" — happens when previous process didn't
        # release the session file before this one started.
        for _attempt in range(6):
            try:
                await self.client.connect()
                break
            except ApiIdInvalidError:
                print(Fore.RED + "❌  Invalid API ID or Hash. Check your my.telegram.org credentials." + Style.RESET_ALL)
                sys.exit(1)
            except Exception as e:
                if "database is locked" in str(e).lower() and _attempt < 5:
                    print(Fore.YELLOW + f"⏳  Session file locked, retrying in 3s… ({_attempt+1}/5)" + Style.RESET_ALL)
                    await asyncio.sleep(3)
                else:
                    print(Fore.RED + f"❌  Connection failed: {e}" + Style.RESET_ALL)
                    sys.exit(1)

        if not await self.client.is_user_authorized():
            await self._login()

        me = await self.client.get_me()
        name = me.first_name or ""
        if me.last_name:
            name += f" {me.last_name}"
        uname = f"@{me.username}" if me.username else f"id={me.id}"
        print(Fore.GREEN + f"✅  Logged in as: {name} ({uname})\n" + Style.RESET_ALL)

        await run_menu(self.client)

    async def _login(self):
        print(Fore.YELLOW + "📱  First-time login — your session will be saved after this.\n" + Style.RESET_ALL)
        phone = input("Enter your phone number (with country code, e.g. +12345678901): ").strip()

        try:
            await self.client.send_code_request(phone)
        except Exception as e:
            print(Fore.RED + f"❌  Failed to send code: {e}" + Style.RESET_ALL)
            sys.exit(1)

        for attempt in range(3):
            code = input("Enter the OTP Telegram sent you: ").strip()
            try:
                await self.client.sign_in(phone, code)
                break
            except PhoneCodeInvalidError:
                print(Fore.RED + "❌  Wrong code. Try again." + Style.RESET_ALL)
            except PhoneCodeExpiredError:
                print(Fore.RED + "❌  Code expired. Requesting a new one..." + Style.RESET_ALL)
                await self.client.send_code_request(phone)
            except SessionPasswordNeededError:
                await self._two_factor()
                break
            except Exception as e:
                print(Fore.RED + f"❌  Sign-in error: {e}" + Style.RESET_ALL)
                sys.exit(1)

        print(Fore.GREEN + "✅  Login successful! Session saved.\n" + Style.RESET_ALL)

    async def _two_factor(self):
        print(Fore.YELLOW + "🔐  Two-step verification is enabled." + Style.RESET_ALL)
        for attempt in range(3):
            password = input("Enter your 2FA password: ").strip()
            try:
                await self.client.sign_in(password=password)
                return
            except PasswordHashInvalidError:
                print(Fore.RED + "❌  Wrong password. Try again." + Style.RESET_ALL)
            except Exception as e:
                print(Fore.RED + f"❌  2FA error: {e}" + Style.RESET_ALL)
                sys.exit(1)
        print(Fore.RED + "❌  Too many failed attempts." + Style.RESET_ALL)
        sys.exit(1)
