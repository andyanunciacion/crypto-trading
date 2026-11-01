"""`python -m backend.alerts`: run the alerts service, and with Telegram set up, the
chat's commands (/status, /pnl, /pause, /kill; commands.py). Also, to set it up and
check it:

- `chat-id`: list the chats that have messaged the bot, to find TELEGRAM_CHAT_ID (send
  the bot any message in Telegram first). Needs only TELEGRAM_BOT_TOKEN.
- `test`: send one message to the chat.
- `summary`: send the daily summary now.
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from datetime import UTC, datetime

import structlog
import telegram
from redis.asyncio import Redis
from telegram.error import Conflict

from backend.alerts.commands import CommandBot, ShownKills
from backend.alerts.service import AlertsService, beat
from backend.alerts.settings import AlertsSettings, load_alerts_settings
from backend.alerts.telegram import DrySender, Sender, TelegramSender
from backend.alerts.watch import EngineWatcher
from shared.db.session import make_engine
from shared.logs import LogMirror, configure_logging

log = structlog.get_logger()

TEST_MESSAGE = "crypto-trading: a test message. Alerts will arrive in this chat."


def make_sender(settings: AlertsSettings) -> Sender:
    token = settings.telegram_bot_token
    if token is None or settings.telegram_chat_id is None:
        log.warning(
            "alerts.not_configured",
            hint="messages are only logged; set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "(python -m backend.alerts chat-id finds the chat)",
        )
        return DrySender()
    secret = token.get_secret_value()
    return TelegramSender(telegram.Bot(secret), settings.telegram_chat_id, secret)


async def run(settings: AlertsSettings, mirror: LogMirror) -> None:
    """The service until SIGTERM, its own lines on the Logs page too (D49)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # As in engine/main.py: PID 1 in a container ignores SIGTERM without a handler.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    log.info("alerts.starting", configured=settings.configured)
    redis = Redis.from_url(settings.redis_url)
    db = make_engine(settings.database_url)
    sender = make_sender(settings)
    shown_kills = ShownKills()
    try:
        service = AlertsService(redis, db, sender, mode=settings.mode, shown_kills=shown_kills)
        await service.write_status()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(beat(service, stop))
            tasks.create_task(mirror.ship(redis, stop))  # ends after `stop`
            # The engine's heartbeat (P7-3); it ends after `stop`.
            tasks.create_task(EngineWatcher(service, redis).run(stop))
            running = [tasks.create_task(service.run())]
            # Commands (P7-2) only with a real bot and chat: a dry run has no one to answer.
            if isinstance(sender, TelegramSender) and settings.telegram_chat_id is not None:
                commands = CommandBot(
                    sender.bot,
                    sender,
                    redis,
                    db,
                    chat_id=settings.telegram_chat_id,
                    mode=settings.mode,
                    shown_kills=shown_kills,
                )
                running.append(tasks.create_task(commands.run()))
            await stop.wait()
            for task in running:
                task.cancel()
    finally:
        with contextlib.suppress(Exception):
            await mirror.flush(redis)
        await sender.close()
        await redis.aclose()
        await db.dispose()
    log.info("alerts.stopping")


async def chat_ids(settings: AlertsSettings) -> int:
    if settings.telegram_bot_token is None:
        print("Set TELEGRAM_BOT_TOKEN in .env first (from @BotFather).", file=sys.stderr)
        return 1
    try:
        async with telegram.Bot(settings.telegram_bot_token.get_secret_value()) as bot:
            updates = await bot.get_updates()
    except Conflict:
        # Telegram lets one reader at a time wait for a bot's messages, and the running
        # service is one once the chat is set (its commands, P7-2).
        print(
            "The alerts service is reading the bot's messages: run "
            "`docker compose stop alerts` first, then this again.",
            file=sys.stderr,
        )
        return 1
    chats = {u.effective_chat.id: u.effective_chat for u in updates if u.effective_chat}
    if not chats:
        print("No messages yet: send your bot any message in Telegram, then run this again.")
        return 1
    for chat in chats.values():
        name = chat.title or chat.username or chat.full_name or ""
        print(f"{chat.id}\t{chat.type}\t{name}")
    print("Put yours in .env as TELEGRAM_CHAT_ID, then restart the alerts service.")
    return 0


async def test(settings: AlertsSettings) -> int:
    sender = make_sender(settings)
    try:
        delivered = await sender.send(TEST_MESSAGE)
    finally:
        await sender.close()
    if not sender.configured:
        print("Not sent: Telegram isn't set up (see .env.example).", file=sys.stderr)
        return 1
    print("Sent." if delivered else f"Not sent: {sender.last_error}")
    return 0 if delivered else 1


async def summary(settings: AlertsSettings) -> int:
    redis = Redis.from_url(settings.redis_url)
    db = make_engine(settings.database_url)
    sender = make_sender(settings)
    try:
        service = AlertsService(redis, db, sender, mode=settings.mode)
        delivered = await service.send_summary(datetime.now(UTC))
    finally:
        await sender.close()
        await redis.aclose()
        await db.dispose()
    return 0 if delivered else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.alerts", description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("chat-id", help="list the chats that have messaged the bot")
    commands.add_parser("test", help="send a test message")
    commands.add_parser("summary", help="send the daily summary now")
    args = parser.parse_args(argv)
    # First, so a bad config stops it before it does anything else.
    settings = load_alerts_settings()
    # httpx logs each request's URL at INFO, and the bot token is in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command is None:
        mirror = LogMirror("alerts")
        configure_logging(settings.log_level, mirror=mirror)
        asyncio.run(run(settings, mirror))
        return
    configure_logging(settings.log_level, sys.stderr)  # stdout is the command's answer
    step = {"chat-id": chat_ids, "test": test, "summary": summary}[args.command]
    sys.exit(asyncio.run(step(settings)))


if __name__ == "__main__":
    main()
