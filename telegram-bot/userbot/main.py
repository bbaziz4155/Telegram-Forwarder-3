import os
import sys
import asyncio
import logging

# Add parent dir to path so we can import from telegram-bot/
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from userbot import UserBot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

if __name__ == "__main__":
    bot = UserBot()
    asyncio.run(bot.run())
