"""Telegram commands (backend/alerts/commands.py, P7-2) on the test database and Redis.

A fake Telegram hands the bot real `telegram.Update`s and records what it sends. A
stand-in for the engine pops each command off the Redis list and replies, as the
engine's CommandListener does; the engine's side (a kill stops every bot, whoever sent
it) is tested in engine/tests/test_kill_switch.py and test_bot_control.py.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import URL, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs
from telegram import BotCommandScope, BotCommandScopeChat, InlineKeyboardMarkup, Update
from telegram.error import Conflict, NetworkError

from backend.alerts import replies
from backend.alerts.commands import POLL_SECONDS, SOURCE, CommandBot, ShownKills
from backend.alerts.telegram import TelegramSender
from shared.backoff import Backoff
from shared.commands import (
    COMMAND,
    COMMANDS_KEY,
    BotCommand,
    BotReply,
    Command,
    KillCommand,
    KillReply,
    reply_key,
)
from shared.db.models import BotRow, PaperBalanceRow
from shared.db.session import make_engine
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat

T0 = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)  # 10:00 in Manila
CHAT = 42  # the owner's
STRANGER = 999
TOKEN = "123456:SECRET-token-from-botfather"
BTC = "BTC/USDT"


# --- Telegram, faked --------------------------------------------------------------------


class FakeTelegram:
    def __init__(self) -> None:
        # What each get_updates returns, in turn: a batch, or an exception to raise.
        self.batches: list[Sequence[Update] | BaseException] = []
        self.polls: list[tuple[int | None, int | None]] = []  # (offset, timeout)
        self.sent: list[tuple[str, InlineKeyboardMarkup | None]] = []
        self.edited: list[tuple[int, str]] = []
        self.answered: list[str] = []
        self.menus: list[tuple[list[tuple[str, str]], BotCommandScope | None]] = []

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> object:
        assert chat_id == CHAT, "only ever the owner's chat"
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> object:
        assert chat_id == CHAT
        self.edited.append((message_id, text))
        return True

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int | None = None,  # noqa: ASYNC109 (as telegram.Bot names it)
        allowed_updates: Sequence[str] | None = None,
    ) -> Sequence[Update]:
        assert allowed_updates is not None and set(allowed_updates) == {"message", "callback_query"}
        self.polls.append((offset, timeout))
        batch = self.batches.pop(0) if self.batches else ()
        if isinstance(batch, BaseException):
            raise batch
        return batch

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> object:
        self.answered.append(callback_query_id)
        return True

    async def set_my_commands(
        self, commands: Sequence[tuple[str, str]], scope: BotCommandScope | None = None
    ) -> object:
        self.menus.append((list(commands), scope))
        return True

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.sent]

    def buttons(self, index: int = -1) -> list[list[tuple[str, str]]]:
        markup = self.sent[index][1]
        assert markup is not None, "that message has no buttons"
        return [[(b.text, str(b.callback_data)) for b in row] for row in markup.inline_keyboard]


_ids = iter(range(1, 10_000))


def message(text: str, *, chat: int = CHAT, date: datetime = T0) -> Update:
    update_id = next(_ids)
    return Update.de_json(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(date.timestamp()),
                "chat": {"id": chat, "type": "private"},
                "from": {"id": chat, "is_bot": False, "first_name": "A", "username": f"u{chat}"},
                "text": text,
            },
        },
        None,
    )


def press(data: str, message_id: int, *, chat: int = CHAT) -> Update:
    update_id = next(_ids)
    return Update.de_json(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cq-{update_id}",
                "from": {"id": chat, "is_bot": False, "first_name": "A"},
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": int(T0.timestamp()),
                    "chat": {"id": chat, "type": "private"},
                    "text": "a prompt",
                },
            },
        },
        None,
    )


# --- The engine, stood in for -----------------------------------------------------------


def killed(command: Command) -> KillReply | BotReply | None:
    if isinstance(command, KillCommand):
        return KillReply(id=command.id, ok=True, stopped=[1, 2, 3], canceled=["b1-x-b"], took_ms=9)
    return BotReply(id=command.id, ok=True, bot_id=getattr(command, "bot_id", None), took_ms=3)


async def engine(
    redis: Redis, count: int, answer: Callable[[Command], KillReply | BotReply | None] = killed
) -> list[Command]:
    """Pop `count` commands, as the engine's CommandListener does, replying to each."""
    commands = []
    for _ in range(count):
        popped = await redis.brpop([COMMANDS_KEY], timeout=5)
        assert popped is not None, "no command was sent"
        command = COMMAND.validate_json(popped[1])
        commands.append(command)
        reply = answer(command)
        if reply is not None:
            await redis.lpush(reply_key(command.id), reply.model_dump_json())
    return commands


# --- The setup --------------------------------------------------------------------------


@dataclass
class Clock:
    now: datetime = T0
    mono: float = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono


@dataclass
class Setup:
    db: AsyncEngine
    redis: Redis
    telegram: FakeTelegram
    clock: Clock
    shown: ShownKills
    slept: list[float] = field(default_factory=list)

    def bot(self, *, reply_within: float = 2.0) -> CommandBot:
        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)

        sender = TelegramSender(self.telegram, CHAT, TOKEN, min_interval=0, sleep=sleep)
        return CommandBot(
            self.telegram,
            sender,
            self.redis,
            self.db,
            chat_id=CHAT,
            mode="paper",
            shown_kills=self.shown,
            clock=self.clock,
            monotonic=self.clock.monotonic,
            sleep=sleep,
            backoff=Backoff(rng=random.Random(0)),
            reply_within=reply_within,
        )

    async def queued(self) -> list[Command]:
        return [COMMAND.validate_json(raw) for raw in await self.redis.lrange(COMMANDS_KEY, 0, -1)]


@pytest.fixture
async def setup(db_url: URL, redis_url: str) -> AsyncIterator[Setup]:
    db = make_engine(db_url)
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events, strategy_switches RESTART IDENTITY CASCADE"
            )
        )
        bot: dict[str, Any] = {"exchange": "mexc", "symbol": BTC, "timeframe": "1h"}
        await conn.execute(
            insert(BotRow),
            [
                {**bot, "name": "alpha", "status": "running", "paused_reason": None},
                {**bot, "name": "beta", "status": "running", "paused_reason": None},
                {**bot, "name": "gamma", "status": "paused", "paused_reason": "paused from x"},
            ],
        )
        await conn.execute(
            insert(PaperBalanceRow).values(
                bot_id=1, currency="USDT", free=Decimal("1000"), used=Decimal(0)
            )
        )
    try:
        yield Setup(db, redis, FakeTelegram(), Clock(), ShownKills())
    finally:
        await redis.aclose()
        await db.dispose()


async def prompted(setup: Setup, bot: CommandBot, command: str) -> int:
    """Send `command`, which answers with buttons; the id of that message."""
    setup.telegram.batches.append([message(command)])
    await bot.poll()
    assert setup.telegram.sent[-1][1] is not None, setup.telegram.texts[-1]
    return 1000 + len(setup.telegram.sent)


# --- /kill ------------------------------------------------------------------------------


async def test_kill_asks_first_then_stop_all_bots_sends_the_kill_switch(setup: Setup) -> None:
    bot = setup.bot()

    prompt = await prompted(setup, bot, "/kill")

    assert setup.telegram.texts == [replies.KILL_PROMPT]
    assert setup.telegram.buttons() == [
        [("Stop all bots", replies.KILL_STOP)],
        [("Stop all + close positions", replies.KILL_CLOSE)],
        [("Cancel", replies.CANCEL)],
    ]
    assert await setup.queued() == []  # nothing until a button is pressed

    stand_in = asyncio.create_task(engine(setup.redis, 1))
    setup.telegram.batches.append([press(replies.KILL_STOP, prompt)])
    with capture_logs() as logs:
        await bot.poll()
    [command] = await stand_in

    assert isinstance(command, KillCommand)
    assert (command.source, command.close_positions, command.sent_at) == (SOURCE, False, T0)
    assert setup.telegram.edited == [(prompt, replies.kill_chosen(False))]
    assert setup.telegram.texts[-1].splitlines()[:2] == [
        "Kill switch done",
        "Stopped 3 bots, cancelled 1 orders, in 9 ms",
    ]
    assert len(setup.telegram.answered) == 1  # the button's spinner stopped
    assert str(command.id) in setup.shown  # so its alert isn't sent again
    sent = next(line for line in logs if line["event"] == "alerts.command.kill_sent")
    assert (sent["log_level"], sent["close_positions"]) == ("warning", False)


async def test_stop_all_and_close_positions_asks_the_kill_switch_to_sell(setup: Setup) -> None:
    bot = setup.bot()
    prompt = await prompted(setup, bot, "/kill")

    stand_in = asyncio.create_task(engine(setup.redis, 1))
    setup.telegram.batches.append([press(replies.KILL_CLOSE, prompt)])
    await bot.poll()
    [command] = await stand_in

    assert isinstance(command, KillCommand) and command.close_positions


async def test_cancel_sends_nothing(setup: Setup) -> None:
    bot = setup.bot()
    prompt = await prompted(setup, bot, "/kill")

    setup.telegram.batches.append([press(replies.CANCEL, prompt)])
    await bot.poll()

    assert setup.telegram.edited == [(prompt, replies.CANCELLED)]
    assert await setup.queued() == []


async def test_buttons_work_once_and_only_for_two_minutes(setup: Setup) -> None:
    bot = setup.bot()
    prompt = await prompted(setup, bot, "/kill")
    stand_in = asyncio.create_task(engine(setup.redis, 1))
    # A double tap: the second press finds the buttons used.
    setup.telegram.batches.append(
        [press(replies.KILL_STOP, prompt), press(replies.KILL_STOP, prompt)]
    )
    await bot.poll()
    await stand_in
    assert setup.telegram.edited[-1] == (prompt, replies.EXPIRED)

    late = await prompted(setup, bot, "/kill")
    setup.clock.mono += 121
    setup.telegram.batches.append([press(replies.KILL_STOP, late)])
    await bot.poll()

    assert setup.telegram.edited[-1] == (late, replies.EXPIRED)
    assert await setup.queued() == []  # only the first press sent a kill


async def test_buttons_from_before_a_restart_have_expired(setup: Setup) -> None:
    prompt = await prompted(setup, setup.bot(), "/kill")
    restarted = setup.bot()

    setup.telegram.batches.append([press(replies.KILL_STOP, prompt)])
    await restarted.poll()

    assert setup.telegram.edited == [(prompt, replies.EXPIRED)]
    assert await setup.queued() == []


async def test_with_no_reply_from_the_engine_the_kill_stays_queued(setup: Setup) -> None:
    bot = setup.bot(reply_within=0.3)
    prompt = await prompted(setup, bot, "/kill")

    setup.telegram.batches.append([press(replies.KILL_STOP, prompt)])
    await bot.poll()

    assert setup.telegram.texts[-1] == replies.kill_no_reply(0.3)
    [queued] = await setup.queued()
    assert isinstance(queued, KillCommand) and queued.source == SOURCE
    # Not shown, so when the engine carries it out, its alert says what it did.
    assert str(queued.id) not in setup.shown


# --- Only the owner's chat --------------------------------------------------------------


async def test_messages_from_another_chat_are_ignored_and_logged(setup: Setup) -> None:
    bot = setup.bot()
    prompt = await prompted(setup, bot, "/kill")  # the owner's prompt, pressed by a stranger
    before = list(setup.telegram.sent)

    setup.telegram.batches.append(
        [
            message("/kill", chat=STRANGER),
            message("/status", chat=STRANGER),
            message("/pause alpha", chat=STRANGER),
            press(replies.KILL_STOP, prompt, chat=STRANGER),
        ]
    )
    with capture_logs() as logs:
        await bot.poll()

    assert setup.telegram.sent == before  # no reply: it would show the bot is alive
    assert setup.telegram.edited == [] and setup.telegram.answered == []
    assert await setup.queued() == []
    ignored = [line for line in logs if line["event"] == "alerts.command.ignored"]
    assert [line["text"] for line in ignored] == [
        "/kill",
        "/status",
        "/pause alpha",
        replies.KILL_STOP,
    ]
    assert all(line["chat_id"] == STRANGER and line["log_level"] == "warning" for line in ignored)
    assert ignored[0]["username"] == f"u{STRANGER}"


# --- What waited too long ---------------------------------------------------------------


async def test_a_command_waiting_since_before_a_start_isn_t_run(setup: Setup) -> None:
    bot = setup.bot()

    setup.telegram.batches.append([message("/kill", date=T0 - timedelta(minutes=5))])
    await bot.poll()

    assert setup.telegram.texts == [replies.too_old("/kill", T0 - timedelta(minutes=5), T0)]
    assert setup.telegram.sent[0][1] is None  # no buttons


async def test_a_fresh_command_in_the_backlog_is_run(setup: Setup) -> None:
    bot = setup.bot()

    setup.telegram.batches.append([message("/kill", date=T0 - timedelta(seconds=90))])
    await bot.poll()

    assert setup.telegram.texts == [replies.KILL_PROMPT]


async def test_once_caught_up_a_command_is_run_whatever_its_date_says(setup: Setup) -> None:
    # A container clock that drifted ahead (Docker Desktop's can, after the PC sleeps)
    # mustn't refuse a command that just came.
    bot = setup.bot()
    await bot.poll()  # the backlog: nothing

    setup.telegram.batches.append([message("/kill", date=T0 - timedelta(minutes=5))])
    await bot.poll()

    assert setup.telegram.texts == [replies.KILL_PROMPT]
    # The backlog is fetched at once; then each poll waits for the next message.
    assert [timeout for _, timeout in setup.telegram.polls] == [0, POLL_SECONDS]


# --- /pause -----------------------------------------------------------------------------


async def test_pause_by_name_or_id_pauses_that_bot(setup: Setup) -> None:
    bot = setup.bot()
    stand_in = asyncio.create_task(engine(setup.redis, 2))

    setup.telegram.batches.append([message("/pause alpha"), message("/pause 2")])
    await bot.poll()
    first, second = await stand_in

    assert isinstance(first, BotCommand) and isinstance(second, BotCommand)
    assert [(c.action, c.bot_id, c.source) for c in (first, second)] == [
        ("pause", 1, SOURCE),
        ("pause", 2, SOURCE),
    ]
    assert first.expires_at == T0 + timedelta(seconds=2)  # it can't take effect later
    assert setup.telegram.texts == [
        "alpha: paused. No buys; it still sells and keeps its stops.\n"
        "Start a paused bot again on the dashboard's Bots page.",
        "beta: paused. No buys; it still sells and keeps its stops.\n"
        "Start a paused bot again on the dashboard's Bots page.",
    ]


async def test_pause_takes_a_name_in_any_case(setup: Setup) -> None:
    stand_in = asyncio.create_task(engine(setup.redis, 1))
    setup.telegram.batches.append([message("/pause ALPHA")])

    await setup.bot().poll()

    [command] = await stand_in
    assert isinstance(command, BotCommand) and command.bot_id == 1


async def test_pausing_a_bot_that_isn_t_there_lists_the_running_ones(setup: Setup) -> None:
    setup.telegram.batches.append([message("/pause nope")])

    await setup.bot().poll()

    assert setup.telegram.texts == ["There's no bot called “nope”. Running: alpha, beta."]
    assert await setup.queued() == []


async def test_the_engine_s_refusal_is_the_reply(setup: Setup) -> None:
    def refuse(command: Command) -> BotReply:
        return BotReply(id=command.id, ok=False, error="Bot 3 is paused, not running.", took_ms=1)

    stand_in = asyncio.create_task(engine(setup.redis, 1, refuse))
    setup.telegram.batches.append([message("/pause gamma")])

    await setup.bot().poll()
    await stand_in

    assert setup.telegram.texts == ["gamma: not paused. Bot 3 is paused, not running."]


async def test_pause_alone_offers_the_running_bots_and_all_of_them(setup: Setup) -> None:
    bot = setup.bot()

    prompt = await prompted(setup, bot, "/pause")

    assert setup.telegram.texts == [replies.PAUSE_PROMPT]
    assert setup.telegram.buttons() == [  # gamma is paused already
        [("alpha", "pause:1")],
        [("beta", "pause:2")],
        [("All running bots", replies.PAUSE_ALL)],
        [("Cancel", replies.CANCEL)],
    ]

    def beta_refuses(command: Command) -> BotReply:
        ok = getattr(command, "bot_id", None) == 1
        return BotReply(id=command.id, ok=ok, error=None if ok else "busy", took_ms=1)

    stand_in = asyncio.create_task(engine(setup.redis, 2, beta_refuses))
    setup.telegram.batches.append([press(replies.PAUSE_ALL, prompt)])
    await bot.poll()
    commands = await stand_in

    assert [getattr(c, "bot_id", None) for c in commands] == [1, 2]
    assert setup.telegram.edited == [(prompt, replies.pause_chosen("every running bot"))]
    assert setup.telegram.texts[-1].splitlines() == [
        "alpha: paused. No buys; it still sells and keeps its stops.",
        "beta: not paused. busy",
        "Start a paused bot again on the dashboard's Bots page.",
    ]


async def test_a_pause_button_pauses_that_bot(setup: Setup) -> None:
    bot = setup.bot()
    prompt = await prompted(setup, bot, "/pause")

    stand_in = asyncio.create_task(engine(setup.redis, 1))
    setup.telegram.batches.append([press("pause:2", prompt)])
    await bot.poll()
    [command] = await stand_in

    assert isinstance(command, BotCommand) and command.bot_id == 2
    assert setup.telegram.edited == [(prompt, replies.pause_chosen("beta"))]


async def test_a_kill_button_on_a_pause_prompt_does_nothing(setup: Setup) -> None:
    bot = setup.bot()
    prompt = await prompted(setup, bot, "/pause")

    setup.telegram.batches.append([press(replies.KILL_STOP, prompt)])
    await bot.poll()

    assert setup.telegram.edited == [(prompt, replies.EXPIRED)]
    assert await setup.queued() == []


# --- /status, /pnl, /help ---------------------------------------------------------------


async def test_status_and_pnl_read_the_overview(setup: Setup) -> None:
    beat = EngineHeartbeat(
        ts=T0 - timedelta(seconds=4),
        started_at=T0 - timedelta(hours=1),
        mode="paper",
        beat=360,
        exchange="mexc",
        symbols=[],
    )
    await setup.redis.set(ENGINE_HEARTBEAT_KEY, beat.model_dump_json())
    setup.telegram.batches.append([message("/status"), message("/pnl")])

    await setup.bot().poll()

    status, pnl = setup.telegram.texts
    assert status.splitlines()[:3] == [
        "Engine up: heartbeat 4 s ago (paper, mexc), running since 31 Oct 09:00",
        "Bots: running alpha, beta; paused gamma",
        "  gamma paused: paused from x",
    ]
    assert pnl.splitlines()[0] == "Equity 1,000 USDT"
    assert await setup.queued() == []


async def test_status_with_the_database_down_still_says_how_the_engine_is(
    setup: Setup, db_url: URL
) -> None:
    down = make_engine(db_url.set(port=1))  # nothing listens there
    setup.db = down
    try:
        setup.telegram.batches.append([message("/status")])
        await setup.bot().poll()
    finally:
        await down.dispose()

    assert setup.telegram.texts == [replies.status_without_db(None, T0)]


@pytest.mark.parametrize("text", ["/start", "/help", "hello"])
async def test_start_and_help_list_the_commands_and_start_nothing(setup: Setup, text: str) -> None:
    setup.telegram.batches.append([message(text)])

    await setup.bot().poll()

    assert setup.telegram.texts == [replies.HELP]
    assert await setup.queued() == []


async def test_an_unknown_command_gets_the_list(setup: Setup) -> None:
    setup.telegram.batches.append([message("/resume")])

    await setup.bot().poll()

    assert setup.telegram.texts == [replies.unknown("resume")]


# --- Polling ----------------------------------------------------------------------------


async def test_each_update_is_handled_once_and_the_menu_set_once(setup: Setup) -> None:
    bot = setup.bot()
    first, second = message("/help"), message("/help")

    setup.telegram.batches.append([first, second])
    await bot.poll()
    await bot.poll()

    # The second poll asks for what comes after both, which tells Telegram they're done.
    assert setup.telegram.polls[1][0] == second.update_id + 1
    assert len(setup.telegram.texts) == 2
    [(menu, scope)] = setup.telegram.menus
    assert [name for name, _ in menu] == ["status", "pnl", "pause", "kill"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == CHAT  # this chat only


async def test_polling_errors_are_retried_said_once_and_never_show_the_token(
    setup: Setup,
) -> None:
    bot = setup.bot()
    setup.telegram.batches += [
        NetworkError(f"httpx.ConnectError: https://api.telegram.org/bot{TOKEN}/getUpdates"),
        Conflict("terminated by other getUpdates request"),
        [message("/help")],
        [],
        asyncio.CancelledError(),  # ends the loop, as stopping the service does
    ]

    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await bot.run()

    events = [line["event"] for line in logs]
    assert events.count("alerts.commands.poll_failed") == 1
    assert events.count("alerts.commands.poll_recovered") == 1
    assert all(TOKEN not in repr(line) for line in logs)
    assert len(setup.slept) == 2  # backed off after each failure
    assert setup.telegram.texts == [replies.HELP]
    # After an outage the next batch is a backlog again, fetched at once.
    assert [timeout for _, timeout in setup.telegram.polls] == [0, 0, 0] + [POLL_SECONDS] * 2


async def test_a_reply_that_fails_is_said_and_the_next_update_still_runs(setup: Setup) -> None:
    bot = setup.bot()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("redis went away")

    bot._status = broken  # type: ignore[method-assign]
    setup.telegram.batches.append([message("/status"), message("/help")])
    with capture_logs() as logs:
        await bot.poll()

    assert setup.telegram.texts == [
        "That didn't work (RuntimeError). The Logs page has the details.",
        replies.HELP,
    ]
    assert any(line["event"] == "alerts.command.failed" for line in logs)
