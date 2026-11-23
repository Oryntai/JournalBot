import argparse
import asyncio
import base64
import binascii
import logging
import os
import re
import secrets
import signal
import time
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from dotenv import load_dotenv

from .bot import JournalBot
from .store import DAY, MAX_ID, Store

LOG = logging.getLogger("journal_bot")
STARTUP_TIMEOUT = 12


def archive_config():
    owner = os.getenv("TELEGRAM_OWNER_ID", "")
    if not re.fullmatch(r"[0-9]{1,16}", owner) or not 0 < int(owner) <= MAX_ID:
        raise ValueError("Укажи TELEGRAM_OWNER_ID. Узнать его можно через python -m journal_bot identify.")
    encoded = os.getenv("ARCHIVE_KEY_BASE64", "")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32 or base64.b64encode(key).decode() != encoded:
        raise ValueError("ARCHIVE_KEY_BASE64 должен быть случайным 32-байтным ключом в base64.")
    days = os.getenv("ARCHIVE_RETENTION_DAYS", "7")
    if not re.fullmatch(r"[0-9]{1,2}", days) or not 1 <= int(days) <= 90:
        raise ValueError("ARCHIVE_RETENTION_DAYS должен быть целым числом от 1 до 90.")
    zone = os.getenv("ARCHIVE_TIMEZONE", "UTC")
    try:
        time_zone = timezone.utc if zone == "UTC" else ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Укажи часовой пояс IANA в ARCHIVE_TIMEZONE, например Asia/Almaty.") from None
    return int(owner), key, int(days) * DAY, time_zone, Path(os.getenv("ARCHIVE_DB") or "data/archive.sqlite")


async def retry_startup(operation):
    for attempt in range(3):
        try:
            return await operation()
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as error:
            if attempt == 2:
                raise
            await asyncio.sleep(max(2**attempt, getattr(error, "retry_after", 0)))


async def identify(bot, username):
    command = f"/start id_{secrets.token_hex(12)}"
    print(f"Отправь в личный чат @{username} ровно эту строку:\n{command}", flush=True)
    offset = None
    while True:
        updates = await bot.get_updates(offset=offset, allowed_updates=["message"], timeout=20, request_timeout=35)
        for update in updates:
            offset = update.update_id + 1
            message = update.message
            if (message and message.chat.type == "private" and message.from_user
                    and message.chat.id == message.from_user.id and message.text == command):
                print(f"TELEGRAM_OWNER_ID={message.from_user.id}", flush=True)
                return


async def run(mode):
    if os.name != "nt":
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Укажи TELEGRAM_BOT_TOKEN в .env.")
    config = None if mode else archive_config()
    async with Bot(token) as bot:
        profile = await retry_startup(lambda: bot.get_me(request_timeout=STARTUP_TIMEOUT))
        webhook = await retry_startup(lambda: bot.get_webhook_info(request_timeout=STARTUP_TIMEOUT))
        if webhook.url:
            raise ValueError("У бота настроен webhook. Для журнала нужен отдельный бот с long polling.")
        if mode == "identify":
            await identify(bot, profile.username)
            return
        if not profile.can_connect_to_business:
            raise ValueError("Включи Secretary Mode в @BotFather.")
        owner, key, retention, time_zone, path = config
        os.umask(0o077)
        path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path, key, owner, profile.id)
        try:
            store.prune(int(time.time()), retention)
            journal = JournalBot(bot, store, owner, retention, time_zone, profile.username)
            await retry_startup(journal.restore)
            LOG.info("Журнал запущен. @%s готов принимать команды.", profile.username)
            await journal.poll()
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description="Telegram Chat Journal")
    parser.add_argument("mode", nargs="?", choices=["identify"])
    mode = parser.parse_args().mode
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    load_dotenv()
    try:
        asyncio.run(run(mode))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    except ValueError as error:
        LOG.error("%s", error)
    except TelegramAPIError as error:
        LOG.error("Telegram API (%s)", type(error).__name__)
    except Exception as error:
        LOG.error("Служба журнала остановилась (%s)", type(error).__name__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
