"""Sending a message to the owner's Telegram chat, or logging it when there's no bot.

`TelegramSender` sends one message at a time with python-telegram-bot (D16), plain
text with no parse mode, so nothing in a message needs escaping. Telegram allows about
one message a second to a chat, so it waits that long between messages, and longer when
Telegram says so (`RetryAfter`, its flood control). A network error is retried with
backoff until the message goes, because an alert held up is better than one lost. A
refusal that trying again can't fix (a wrong token or chat ID, a bot the owner blocked)
drops the message, and the Settings page shows why.

The bot token is in every request's URL, so it must never reach a log: errors are
logged with the token blanked out, and httpx's request log (which prints the URL) is
held at WARNING in __main__.py.
"""

import asyncio
import time
import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.warnings import PTBDeprecationWarning

from shared.backoff import Backoff

log = structlog.get_logger()

MIN_INTERVAL_SECONDS = 1.0  # Telegram's limit for messages to one chat
MAX_LENGTH = 4096  # Telegram's limit for one message


class TelegramBot(Protocol):
    """What we use of `telegram.Bot`, so tests can stand in for Telegram."""

    async def initialize(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def send_message(self, chat_id: int, text: str) -> object: ...


class Sender(Protocol):
    @property
    def configured(self) -> bool: ...  # False: messages are only logged

    @property
    def last_sent_at(self) -> datetime | None: ...

    @property
    def last_error(self) -> str | None: ...

    async def send(self, text: str) -> bool:
        """Send `text`; False if it was dropped (see the module docstring)."""
        ...

    async def close(self) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _retry_after(exc: RetryAfter) -> float:
    """How long flood control asks us to wait, in seconds."""
    # An int in python-telegram-bot 22, with a warning that it'll become a timedelta.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        value = exc.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


def fit(text: str) -> str:
    """`text`, cut to what one Telegram message holds."""
    return text if len(text) <= MAX_LENGTH else text[: MAX_LENGTH - 1] + "…"


class DrySender:
    """No Telegram bot set up: each message is logged (`alerts.unsent`) instead."""

    configured = False
    last_error: str | None = None

    def __init__(self, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self.last_sent_at: datetime | None = None

    async def send(self, text: str) -> bool:
        log.info("alerts.unsent", text=text, hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        self.last_sent_at = self._clock()
        return True

    async def close(self) -> None:
        return None


class TelegramSender:
    configured = True

    def __init__(
        self,
        bot: TelegramBot,
        chat_id: int,
        token: str,
        *,
        backoff: Backoff | None = None,
        min_interval: float = MIN_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._token = token
        self._backoff = backoff or Backoff()
        self._min_interval = min_interval
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._ready = False
        self._sent_at: float | None = None  # monotonic, for the interval
        self.last_sent_at: datetime | None = None
        self.last_error: str | None = None

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>") if self._token else text

    async def send(self, text: str) -> bool:
        failing = False
        while True:
            if self._sent_at is not None:
                wait = self._sent_at + self._min_interval - self._monotonic()
                if wait > 0:
                    await self._sleep(wait)
            try:
                if not self._ready:
                    await self._bot.initialize()  # asks Telegram who the bot is
                    self._ready = True
                await self._bot.send_message(chat_id=self._chat_id, text=fit(text))
            except RetryAfter as exc:
                seconds = _retry_after(exc)
                log.warning("alerts.flood_control", retry_after_s=seconds)
                await self._sleep(seconds)
            except BadRequest as exc:  # a NetworkError subclass, but trying again won't help
                return self._refused(exc)
            except NetworkError as exc:  # includes TimedOut
                delay = self._backoff.next_delay()
                if not failing:
                    log.warning(
                        "alerts.send_failed",
                        error=type(exc).__name__,
                        detail=self._redact(str(exc)),
                        retry_in_s=round(delay, 2),
                    )
                failing = True
                self.last_error = f"can't reach Telegram: {self._redact(str(exc))}"
                await self._sleep(delay)
            except TelegramError as exc:  # InvalidToken, Forbidden, ChatMigrated, ...
                return self._refused(exc)
            else:
                self._backoff.reset()
                self._sent_at = self._monotonic()
                self.last_sent_at = self._clock()
                self.last_error = None
                if failing:
                    log.info("alerts.send_recovered")
                return True

    def _refused(self, exc: TelegramError) -> bool:
        detail = self._redact(str(exc))
        log.warning("alerts.send_refused", error=type(exc).__name__, detail=detail)
        self.last_error = f"Telegram refused the message ({type(exc).__name__}): {detail}"
        return False

    async def close(self) -> None:
        if self._ready:
            await self._bot.shutdown()
            self._ready = False
