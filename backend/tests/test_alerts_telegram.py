"""Sending to Telegram (backend/alerts/telegram.py): one message a second, flood control
waited out, network errors retried until the message goes, refusals dropped, and the bot
token never in a log line. A fake bot stands in for Telegram."""

import asyncio
import random
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs
from telegram import BotCommandScope, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from backend.alerts.settings import AlertsSettings
from backend.alerts.telegram import MAX_LENGTH, DrySender, TelegramSender
from shared.backoff import Backoff

TOKEN = "123456:SECRET-token-from-botfather"
CHAT = 42
T0 = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)  # raised by the next sends, in order
        self.sent: list[tuple[int, str]] = []
        self.markups: list[InlineKeyboardMarkup | None] = []
        self.edited: list[tuple[int, int, str]] = []
        self.initialized = 0
        self.shut_down = 0

    async def initialize(self) -> None:
        self.initialized += 1

    async def shutdown(self) -> None:
        self.shut_down += 1

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> object:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> object:
        if self.failures:
            raise self.failures.pop(0)
        self.edited.append((chat_id, message_id, text))
        return True

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int | None = None,  # noqa: ASYNC109 (as telegram.Bot names it)
        allowed_updates: Sequence[str] | None = None,
    ) -> Sequence[Update]:
        return ()

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> object:
        return True

    async def set_my_commands(
        self, commands: Sequence[tuple[str, str]], scope: BotCommandScope | None = None
    ) -> object:
        return True


class Clock:
    """Monotonic time that moves only when the sender sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Iterator[Clock]:
    yield Clock()


def sender(bot: FakeBot, clock: Clock) -> TelegramSender:
    return TelegramSender(
        bot,
        CHAT,
        TOKEN,
        backoff=Backoff(rng=random.Random(0)),
        clock=lambda: T0,
        monotonic=clock,
        sleep=clock.sleep,
    )


async def test_messages_go_to_the_chat_one_a_second(clock: Clock) -> None:
    bot = FakeBot()
    s = sender(bot, clock)

    assert await s.send("one")
    assert await s.send("two")
    clock.now += 5  # a while later: no wait
    assert await s.send("three")

    assert bot.sent == [(CHAT, "one"), (CHAT, "two"), (CHAT, "three")]
    assert clock.slept == [1.0]
    assert bot.initialized == 1
    assert s.last_sent_at == T0 and s.last_error is None


# RetryAfter's own __init__ warns that `retry_after` will become a timedelta.
@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")
async def test_flood_control_is_waited_out_then_the_message_goes(clock: Clock) -> None:
    bot = FakeBot(RetryAfter(7))
    s = sender(bot, clock)

    assert await s.send("hello")

    assert bot.sent == [(CHAT, "hello")]
    assert clock.slept == [7]


async def test_network_errors_are_retried_with_backoff_until_it_goes(clock: Clock) -> None:
    bot = FakeBot(NetworkError("httpx.ConnectError: down"), TimedOut())
    s = sender(bot, clock)

    with capture_logs() as logs:
        assert await s.send("hello")

    assert bot.sent == [(CHAT, "hello")]
    assert len(clock.slept) == 2 and all(delay > 0 for delay in clock.slept)
    # Said once per outage, and once when it's over.
    assert [line["event"] for line in logs] == ["alerts.send_failed", "alerts.send_recovered"]
    assert s.last_error is None


@pytest.mark.parametrize(
    "refusal", [BadRequest("Chat not found"), Forbidden("bot was blocked by the user")]
)
async def test_a_refusal_trying_again_cant_fix_drops_the_message(
    clock: Clock, refusal: Exception
) -> None:
    bot = FakeBot(refusal)
    s = sender(bot, clock)

    assert not await s.send("hello")

    assert bot.sent == [] and clock.slept == []
    assert s.last_error is not None and type(refusal).__name__ in s.last_error
    assert await s.send("next")  # the next message is tried as usual


async def test_the_token_never_reaches_a_log_line_or_the_status(clock: Clock) -> None:
    leaky = NetworkError(f"httpx.ConnectError: https://api.telegram.org/bot{TOKEN}/sendMessage")
    bot = FakeBot(leaky, BadRequest(f"bad request to bot{TOKEN}"))
    s = sender(bot, clock)

    with capture_logs() as logs:
        await s.send("hello")

    assert logs, "nothing was logged"
    assert all(TOKEN not in repr(line) for line in logs)
    assert s.last_error is not None and TOKEN not in s.last_error
    assert "<token>" in s.last_error


async def test_alerts_and_replies_sent_at_once_still_go_one_a_second(clock: Clock) -> None:
    # The alerts loop and the commands (P7-2) share the sender; without the lock both
    # would see the same last send and skip the wait.
    bot = FakeBot()
    s = sender(bot, clock)

    await asyncio.gather(s.send("alert"), s.send("reply"), s.edit(7, "edited"))

    assert [text for _, text in bot.sent] == ["alert", "reply"]
    assert bot.edited == [(CHAT, 7, "edited")]
    assert clock.slept == [1.0, 1.0]


async def test_a_prompt_carries_its_buttons_and_says_its_message_id(clock: Clock) -> None:
    bot = FakeBot()
    s = sender(bot, clock)

    message_id = await s.prompt("Sure?", [[("Yes", "yes")], [("Cancel", "cancel")]])
    await s.send("plain")

    assert message_id == 101
    markup = bot.markups[0]
    assert markup is not None
    assert [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard] == [
        [("Yes", "yes")],
        [("Cancel", "cancel")],
    ]
    assert bot.markups[1] is None


async def test_a_refused_prompt_has_no_message_id(clock: Clock) -> None:
    bot = FakeBot(Forbidden("bot was blocked by the user"))

    assert await sender(bot, clock).prompt("Sure?", [[("Yes", "yes")]]) is None


async def test_a_long_message_is_cut_to_what_telegram_takes(clock: Clock) -> None:
    bot = FakeBot()

    await sender(bot, clock).send("x" * (MAX_LENGTH + 100))

    assert len(bot.sent[0][1]) == MAX_LENGTH


async def test_closing_shuts_the_bot_down_once_it_was_started(clock: Clock) -> None:
    bot = FakeBot()
    s = sender(bot, clock)
    await s.close()
    assert bot.shut_down == 0

    await s.send("hello")
    await s.close()

    assert bot.shut_down == 1


async def test_with_no_bot_set_up_messages_are_only_logged() -> None:
    dry = DrySender(clock=lambda: T0)

    with capture_logs() as logs:
        assert await dry.send("hello")

    assert not dry.configured
    assert logs[0]["event"] == "alerts.unsent" and logs[0]["text"] == "hello"
    assert dry.last_sent_at == T0


def settings(**env: Any) -> AlertsSettings:
    base: dict[str, Any] = {"postgres_user": "t", "postgres_password": "t", "postgres_db": "t"}
    return AlertsSettings(_env_file=None, **base, **env)


def test_alerts_are_sent_only_with_a_token_and_a_chat() -> None:
    assert settings(telegram_bot_token=TOKEN, telegram_chat_id=CHAT).configured
    assert not settings().configured
    # A token alone is how `chat-id` is run, before the chat is known.
    assert not settings(telegram_bot_token=TOKEN).configured
    # Blank, as .env.example has them, is unset.
    assert not settings(telegram_bot_token="", telegram_chat_id="").configured


def test_a_chat_without_a_token_is_refused() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        settings(telegram_chat_id=CHAT)


def test_the_token_is_hidden_when_the_settings_are_printed() -> None:
    assert TOKEN not in repr(settings(telegram_bot_token=TOKEN, telegram_chat_id=CHAT))
    structlog.reset_defaults()
