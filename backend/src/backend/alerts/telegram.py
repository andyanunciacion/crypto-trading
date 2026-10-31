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

The alerts and the replies to commands (commands.py, P7-2) share one sender, and a lock
takes their messages one at a time, so together they keep to the one-a-second limit.
"""

import asyncio
import time
import warnings
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from telegram import BotCommandScope, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.warnings import PTBDeprecationWarning

from shared.backoff import Backoff

log = structlog.get_logger()

MIN_INTERVAL_SECONDS = 1.0  # Telegram's limit for messages to one chat
MAX_LENGTH = 4096  # Telegram's limit for one message

# Buttons under a message: rows of (label, data). Pressing one sends us its data, which
# Telegram limits to 64 bytes.
Buttons = Sequence[Sequence[tuple[str, str]]]


class TelegramBot(Protocol):
    """What we use of `telegram.Bot`, so tests can stand in for Telegram."""

    async def initialize(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> object: ...
    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> object: ...
    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int | None = None,  # noqa: ASYNC109 (Telegram's long-poll wait, as PTB names it)
        allowed_updates: Sequence[str] | None = None,
    ) -> Sequence[Update]: ...
    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> object: ...
    async def set_my_commands(
        self, commands: Sequence[tuple[str, str]], scope: BotCommandScope | None = None
    ) -> object: ...


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


def markup(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in buttons
        ]
    )


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
        self._lock = asyncio.Lock()  # one message at a time, alerts and replies alike
        self._sent_at: float | None = None  # monotonic, for the interval
        self.last_sent_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def bot(self) -> TelegramBot:
        return self._bot

    def redact(self, text: str) -> str:
        return text.replace(self._token, "<token>") if self._token else text

    async def ready(self) -> None:
        """Make sure the bot is set up (it asks Telegram who it is); raises if it can't."""
        if not self._ready:
            await self._bot.initialize()
            self._ready = True

    async def send(self, text: str, *, buttons: Buttons | None = None) -> bool:
        """Send `text` (with `buttons` under it); False if it was dropped."""
        delivered, _ = await self._deliver(
            lambda: self._bot.send_message(
                chat_id=self._chat_id, text=fit(text), reply_markup=markup(buttons)
            )
        )
        return delivered

    async def prompt(self, text: str, buttons: Buttons) -> int | None:
        """Send `text` with `buttons`; the message's id (to know its buttons when they're
        pressed), or None if it was dropped."""
        delivered, message = await self._deliver(
            lambda: self._bot.send_message(
                chat_id=self._chat_id, text=fit(text), reply_markup=markup(buttons)
            )
        )
        message_id = getattr(message, "message_id", None)
        return message_id if delivered and isinstance(message_id, int) else None

    async def edit(self, message_id: int, text: str) -> bool:
        """Replace the text of message `message_id`, taking its buttons away."""
        delivered, _ = await self._deliver(
            lambda: self._bot.edit_message_text(
                fit(text), chat_id=self._chat_id, message_id=message_id
            )
        )
        return delivered

    async def _deliver(self, call: Callable[[], Awaitable[object]]) -> tuple[bool, object]:
        """Make `call` (one message) until it goes, as the module docstring says; whether
        it went, and what Telegram answered."""
        async with self._lock:
            return await self._deliver_locked(call)

    async def _deliver_locked(self, call: Callable[[], Awaitable[object]]) -> tuple[bool, object]:
        failing = False
        while True:
            if self._sent_at is not None:
                wait = self._sent_at + self._min_interval - self._monotonic()
                if wait > 0:
                    await self._sleep(wait)
            try:
                await self.ready()
                answer = await call()
            except RetryAfter as exc:
                seconds = _retry_after(exc)
                log.warning("alerts.flood_control", retry_after_s=seconds)
                await self._sleep(seconds)
            except BadRequest as exc:  # a NetworkError subclass, but trying again won't help
                return self._refused(exc), None
            except NetworkError as exc:  # includes TimedOut
                delay = self._backoff.next_delay()
                if not failing:
                    log.warning(
                        "alerts.send_failed",
                        error=type(exc).__name__,
                        detail=self.redact(str(exc)),
                        retry_in_s=round(delay, 2),
                    )
                failing = True
                self.last_error = f"can't reach Telegram: {self.redact(str(exc))}"
                await self._sleep(delay)
            except TelegramError as exc:  # InvalidToken, Forbidden, ChatMigrated, ...
                return self._refused(exc), None
            else:
                self._backoff.reset()
                self._sent_at = self._monotonic()
                self.last_sent_at = self._clock()
                self.last_error = None
                if failing:
                    log.info("alerts.send_recovered")
                return True, answer

    def _refused(self, exc: TelegramError) -> bool:
        detail = self.redact(str(exc))
        log.warning("alerts.send_refused", error=type(exc).__name__, detail=detail)
        self.last_error = f"Telegram refused the message ({type(exc).__name__}): {detail}"
        return False

    async def close(self) -> None:
        if self._ready:
            await self._bot.shutdown()
            self._ready = False
