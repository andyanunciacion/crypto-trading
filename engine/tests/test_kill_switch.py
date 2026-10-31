"""The kill switch (risk/kill.py, control/, D43), against a real TimescaleDB and Redis
(see shared.testing), a real PaperDesk and BotRunner. The market and the latest price
are fakes, as in test_bot_runner.py, with the same numbers: EMA crossover 3/5, 30 flat
candles at 100, then a close at 110 is a BUY. With 1000 USDT, all-in, that buys 9.0768
BTC at 110 (limit 110.06). Slippage 0.05%: a sell at bid B is limited to B x 0.9995,
rounded down to 0.01.

Commands go through Redis exactly as the CLI sends them; the API isn't involved.
"""

import argparse
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import URL, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.bots.runner import BotRunner
from engine.bots.store import Bot, create_bot, load_bots, open_orders
from engine.control.kill import run as run_cli
from engine.control.listener import CommandListener
from engine.exchange.models import Candle, MarketRules, OrderRequest, Timeframe
from engine.marketdata.store import CandleStore, Series
from engine.risk.kill import KillSwitch
from engine.risk.paper import PaperDesk
from engine.risk.stops import NO_STOPS, Percent, Stops
from engine.strategies.base import Position
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from shared.commands import (
    COMMANDS_KEY,
    Closing,
    KillCommand,
    KillReply,
    NotClosed,
    reply_key,
    send,
)
from shared.db.models import BotRow, OrderRow, PositionRow, RiskEventRow
from shared.db.session import make_engine
from shared.prices import PriceUpdate
from shared.testing import set_limits

BTC = "BTC/USDT"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
SERIES = Series("mexc", BTC, "1m")
RULES = MarketRules(
    symbol=BTC,
    amount_step=Decimal("0.0001"),
    price_step=Decimal("0.01"),
    min_amount=Decimal("0.0001"),
    min_cost=Decimal("1"),
)
STOP5 = Stops(stop_loss=Percent(percent=Decimal("5")))  # 110 x 0.95 = 104.5


def d(value: str) -> Decimal:
    return Decimal(value)


def candle(i: int, close: str = "100") -> Candle:
    price = d(close)
    return Candle(ts=T0 + i * MINUTE, open=price, high=price, low=price, close=price, volume=d("1"))


@dataclass
class Clock:
    now: datetime = T0 + 30 * MINUTE + timedelta(seconds=2)  # candle 29 has just closed

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeMarket:
    rest: list[Candle] = field(default_factory=lambda: [candle(i) for i in range(30)])
    exchange_id: str = "mexc"

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        rows = [c for c in self.rest if since is None or c.ts >= since]
        return rows[:limit]

    async def market_rules(self, symbol: str) -> MarketRules:
        assert symbol == BTC
        return RULES

    async def watch_candles(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        raise AssertionError("tests feed candles themselves")

    async def close_stream(self, symbol: str, timeframe: Timeframe | None = None) -> None:
        return None


@dataclass
class FakeQuotes:
    clock: Clock
    price: PriceUpdate | None = None
    gate: asyncio.Event | None = None  # if set, the next read waits for it once

    def set(self, *, bid: str, size: str = "50", ask: str | None = None) -> None:
        self.price = PriceUpdate(
            exchange="mexc",
            symbol=BTC,
            bid=d(bid),
            ask=d(bid) + d("0.1") if ask is None else d(ask),
            bid_size=d(size),
            ask_size=d(size),
            ts=self.clock.now,
            received_at=self.clock.now,
            source="ws",
        )

    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        if self.gate is not None:
            gate, self.gate = self.gate, None
            await gate.wait()
        return self.price


@dataclass
class Setup:
    db: AsyncEngine
    redis: Redis
    clock: Clock
    quotes: FakeQuotes
    market: FakeMarket
    desk: PaperDesk
    kill_switch: KillSwitch

    def runner(self, bots: Sequence[Bot], **options: Any) -> BotRunner:
        runner = BotRunner(
            bots,
            self.desk.risk,
            self.market,
            self.quotes,
            CandleStore(self.db),
            self.db,
            clock=self.clock,
            **options,
        )
        self.kill_switch.runner = runner
        return runner

    async def bot(self, name: str, stops: Stops = NO_STOPS) -> Bot:
        bot_id = await create_bot(
            self.db,
            name=name,
            exchange="mexc",
            symbol=BTC,
            timeframe="1m",
            strategy=EmaCrossover(EmaCrossoverParams(fast=3, slow=5)),
            created_by="test",
            stops=stops,
        )
        await self.desk.fund(bot_id, "USDT", d("1000"))
        (bot,) = [b for b in await load_bots(self.db, "mexc") if b.id == bot_id]
        return bot

    async def buy_signal(self, runner: BotRunner, *, size: str = "50") -> None:
        """Candle 30 closes at 110: every bot on the series BUYs at the 110 ask."""
        await runner.warm_up(SERIES)
        self.clock.now += MINUTE
        self.quotes.set(bid="109.9", ask="110", size=size)
        await runner.on_closed(SERIES, candle(30, "110"))

    async def price(self, runner: BotRunner, *, bid: str, size: str = "50") -> None:
        """A new price a second on, as the engine's feed hands it out: fills, then the
        stops, then the kill switch's sells."""
        self.clock.now += timedelta(seconds=1)
        self.quotes.set(bid=bid, size=size)
        assert self.quotes.price is not None
        await self.desk.on_price(self.quotes.price)
        await runner.on_price(self.quotes.price)
        await self.kill_switch.on_price(self.quotes.price)

    def command(self, *, close_positions: bool = False) -> KillCommand:
        return KillCommand(close_positions=close_positions, source="test", sent_at=self.clock.now)

    async def statuses(self) -> list[tuple[str, str, str | None]]:
        async with self.db.connect() as conn:
            rows = await conn.execute(
                select(BotRow.name, BotRow.status, BotRow.stopped_reason).order_by(BotRow.id)
            )
            return [(r.name, r.status, r.stopped_reason) for r in rows]

    async def restart(self) -> BotRunner:
        """A new desk, kill switch and runner on the same database, as after a crash."""
        self.desk = PaperDesk(self.db, self.quotes, self.market, clock=self.clock)
        self.kill_switch = KillSwitch(
            self.desk.risk, self.db, self.market, self.quotes, clock=self.clock
        )
        runner = self.runner(await load_bots(self.db, "mexc"))
        await runner.recover()
        return runner


@pytest.fixture
async def setup(db_url: URL, redis_url: str) -> AsyncIterator[Setup]:
    db = make_engine(db_url)
    async with db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events, strategy_switches, candles RESTART IDENTITY"
            )
        )
    await set_limits(db)  # off: these tests aren't about them
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    clock = Clock()
    quotes = FakeQuotes(clock)
    market = FakeMarket()
    desk = PaperDesk(db, quotes, market, clock=clock)
    kill_switch = KillSwitch(desk.risk, db, market, quotes, clock=clock)
    yield Setup(db, redis, clock, quotes, market, desk, kill_switch)
    await redis.aclose()
    await db.dispose()


@asynccontextmanager
async def listening(setup: Setup) -> AsyncIterator[None]:
    """The engine's command listener, running in the background."""
    task = asyncio.create_task(CommandListener(setup.redis, setup.kill_switch).run())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def order_count(db: AsyncEngine) -> int:
    async with db.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(OrderRow))).scalar_one()


async def exit_of(setup: Setup, client_order_id: str) -> tuple[str, Decimal, Any]:
    order = await setup.desk.risk.get_order(client_order_id)
    assert order is not None
    return order.status, order.filled, order.reason["exit"]


BUY_1 = "b1-202610221230-b"
BUY_2 = "b2-202610221230-b"


# --- Stopping ----------------------------------------------------------------------------


async def test_a_kill_over_redis_stops_every_bot_within_seconds(setup: Setup) -> None:
    a = await setup.bot("btc-a", stops=STOP5)
    b = await setup.bot("btc-b")
    c = await setup.bot("btc-c")
    async with setup.db.begin() as conn:
        await conn.execute(
            update(BotRow)
            .where(BotRow.id == c.id)
            .values(status="paused", paused_reason="test", paused_at=setup.clock.now)
        )
    runner = setup.runner([a, b, c])
    # Only 4 BTC at the ask: bot a buys those and waits for the rest, bot b gets none,
    # and paused bot c's buy is blocked.
    await setup.buy_signal(runner, size="4")
    assert await open_orders(setup.db, mode="paper") == [BUY_1, BUY_2]

    command = setup.command()
    async with listening(setup), asyncio.timeout(5):
        reply = await send(setup.redis, command, reply_within=5)

    assert reply is not None and reply.ok
    assert reply.stopped == [a.id, b.id, c.id]
    assert reply.canceled == [BUY_1, BUY_2]
    assert (reply.closing, reply.not_closed) == ([], [])
    assert reply.took_ms < 2000  # DESIGN's budget for the engine to acknowledge a kill
    why = f"kill switch from test (command {command.id})"
    assert await setup.statuses() == [
        ("btc-a", "stopped", why),
        ("btc-b", "stopped", why),
        ("btc-c", "stopped", why),
    ]
    async with setup.db.connect() as conn:
        paused = (await conn.execute(select(BotRow.paused_reason))).scalars().all()
    assert paused == [None, None, None]
    assert await open_orders(setup.db, mode="paper") == []
    # Not asked to close: bot a keeps the 4 BTC it bought.
    assert await setup.desk.risk.position(a.id, BTC) == Position(
        amount=d("4"), avg_entry=d("110"), last_entry=d("110")
    )

    # The bots never act again: not on the cross down (a SELL for bot a), not on a bid
    # below bot a's 104.5 stop.
    orders = await order_count(setup.db)
    setup.clock.now += MINUTE
    setup.quotes.set(bid="90")
    await runner.on_closed(SERIES, candle(31, "90"))
    await setup.price(runner, bid="104")
    assert await order_count(setup.db) == orders
    # Whoever else tried, the RiskManager blocks a stopped bot's buy.
    request = OrderRequest(
        symbol=BTC, side="buy", amount=d("1"), price=d("105"), client_order_id="b2-test-b"
    )
    outcome = await setup.desk.risk.submit(b.id, request, {"signal": "test"})
    assert (outcome.order, outcome.decision.rule) == (None, "bot_stopped")


async def test_a_buy_under_way_when_the_kill_comes_is_blocked(setup: Setup) -> None:
    a = await setup.bot("btc-a")
    runner = setup.runner([a])
    await runner.warm_up(SERIES)
    setup.clock.now += MINUTE
    setup.quotes.set(bid="109.9", ask="110")
    # The bot acts on its candle, and stalls reading the price, just before it buys.
    gate = asyncio.Event()
    setup.quotes.gate = gate
    step = asyncio.create_task(runner.on_closed(SERIES, candle(30, "110")))
    async with asyncio.timeout(5):
        while setup.quotes.gate is not None:  # noqa: ASYNC110 (waiting on the step task)
            await asyncio.sleep(0.01)

    kill = asyncio.create_task(setup.kill_switch.kill(setup.command()))
    await asyncio.sleep(0.2)
    assert not kill.done()  # it waits for the bot to finish its candle
    gate.set()
    async with asyncio.timeout(5):
        reply = await kill
        await step

    # The bot was stopped in the database first, so its buy found it stopped.
    assert reply.stopped == [a.id]
    assert await setup.desk.risk.get_order(BUY_1) is None
    async with setup.db.connect() as conn:
        rules = (await conn.execute(select(RiskEventRow.rule))).scalars().all()
    assert rules == ["bot_stopped"]


async def test_a_series_whose_bots_are_all_stopped_ends_its_stream(setup: Setup) -> None:
    a = await setup.bot("btc-a")
    next_candle = asyncio.Event()
    closed = asyncio.Event()

    async def stream(symbol: str, timeframe: Timeframe, last: datetime) -> AsyncGenerator[Candle]:
        try:
            await next_candle.wait()
            yield candle(30, "110")  # a BUY, had the bot been running
            await asyncio.Event().wait()  # no more candles: only stopping ends this
        finally:
            closed.set()

    runner = setup.runner([a], stream=stream)
    task = asyncio.create_task(runner.run())
    try:
        async with asyncio.timeout(5):
            while runner.running != [a.id]:  # noqa: ASYNC110 (waiting on its warm-up)
                await asyncio.sleep(0.01)
            assert await runner.stop([a.id]) == [a.id]
            assert await runner.stop([a.id]) == []  # once is enough
            next_candle.set()
            await closed.wait()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert await setup.desk.risk.get_order(BUY_1) is None


async def test_a_kill_sent_while_the_engine_was_down_runs_before_any_bot_loads(
    setup: Setup,
) -> None:
    a = await setup.bot("btc-a")
    b = await setup.bot("btc-b")
    command = setup.command()
    await setup.redis.lpush(COMMANDS_KEY, command.model_dump_json())

    # What the engine does at startup, before it loads the bots.
    assert await CommandListener(setup.redis, setup.kill_switch).drain() == 1

    assert await load_bots(setup.db, "mexc") == []
    key = reply_key(command.id)
    assert 0 < await setup.redis.ttl(key) <= 3600  # the reply expires if nobody reads it
    popped = await setup.redis.blpop([key], timeout=1)
    assert popped is not None
    assert KillReply.model_validate_json(popped[1]).stopped == [a.id, b.id]


# --- Closing positions -------------------------------------------------------------------


async def bought_and_stopped_out_in_part(setup: Setup) -> tuple[Bot, BotRunner]:
    """Bot a bought 9.0768 at 110; then its stop fired at 104.5, with only 4 BTC wanted
    there: its exit `-x1` sold those 4 and waits for the rest (5.0768)."""
    a = await setup.bot("btc-a", stops=STOP5)
    runner = setup.runner([a])
    await setup.buy_signal(runner)
    await setup.price(runner, bid="104.5", size="4")
    status, filled, exit_ = await exit_of(setup, "b1-202610221230-x1")
    assert (status, filled, exit_["kind"]) == ("open", d("4"), "stop_loss")
    return a, runner


async def test_closing_positions_sells_until_flat(setup: Setup) -> None:
    a, runner = await bought_and_stopped_out_in_part(setup)
    command = setup.command(close_positions=True)

    reply = await setup.kill_switch.kill(command)

    # The stop's exit is cancelled, and the kill switch carries on its numbering. The
    # 4 BTC at 104.5 are taken already, so its exit waits: limit 104.5 x 0.9995 -> 104.44.
    assert reply.stopped == [a.id]
    assert reply.canceled == ["b1-202610221230-x1"]
    assert reply.closing == [
        Closing(
            bot_id=a.id,
            symbol=BTC,
            amount=d("5.0768"),
            client_order_id="b1-202610221230-x2",
            status="open",
            filled=d("0"),
        )
    ]
    assert setup.kill_switch.selling == [(a.id, BTC)]

    # The bid falls below its limit: placed again at 104 x 0.9995 -> 103.94, and filled.
    with capture_logs() as logs:
        await setup.price(runner, bid="104")

    status, filled, exit_ = await exit_of(setup, "b1-202610221230-x2")
    assert (status, filled, exit_["kind"], exit_["command"]) == (
        "canceled",
        d("0"),
        "kill_switch",
        str(command.id),
    )
    status, filled, exit_ = await exit_of(setup, "b1-202610221230-x3")
    assert (status, filled, exit_["kind"], exit_["attempt"]) == (
        "closed",
        d("5.0768"),
        "kill_switch",
        3,
    )
    assert await setup.desk.risk.position(a.id, BTC) is None
    assert setup.kill_switch.selling == []
    assert [log["event"] for log in logs if log["event"].startswith("kill_switch.")] == [
        "kill_switch.exit",
        "kill_switch.closed",
    ]
    # 0.553552 + 4 x 104.5 - 0.418 fee + 5.0768 x 104 - 0.5279872 fee = 945.5947648.
    assert (await setup.desk.risk.balances(a.id))["USDT"].free == d("945.5947648")
    # Flat: later prices do nothing.
    orders = await order_count(setup.db)
    await setup.price(runner, bid="90")
    assert await order_count(setup.db) == orders


async def test_a_restart_carries_on_selling(setup: Setup) -> None:
    a, _ = await bought_and_stopped_out_in_part(setup)
    command = setup.command(close_positions=True)
    await setup.kill_switch.kill(command)  # its exit -x2 waits

    runner = await setup.restart()  # which cancels -x2 (D38)
    assert await setup.kill_switch.resume() == [(a.id, BTC)]
    await setup.price(runner, bid="104.2")

    status, filled, exit_ = await exit_of(setup, "b1-202610221230-x3")
    assert (status, filled, exit_["kind"], exit_["command"]) == (
        "closed",
        d("5.0768"),
        "kill_switch",
        str(command.id),
    )
    assert await setup.desk.risk.position(a.id, BTC) is None


async def test_a_position_the_kill_switch_cant_sell_is_reported_and_left_open(
    setup: Setup,
) -> None:
    a = await setup.bot("btc-a")
    # Made by hand: no buy of the bot opened it, so there's no exit ID to sell it by.
    async with setup.db.begin() as conn:
        await conn.execute(
            insert(PositionRow).values(
                bot_id=a.id,
                mode="paper",
                exchange="mexc",
                symbol=BTC,
                side="long",
                amount=d("1"),
                avg_entry=d("100"),
                fees_paid=d("0"),
                opened_at=setup.clock.now,
                realized_pnl=d("0"),
            )
        )
    setup.quotes.set(bid="100")

    reply = await setup.kill_switch.kill(setup.command(close_positions=True))

    assert reply.ok and reply.stopped == [a.id]
    assert reply.not_closed == [
        NotClosed(bot_id=a.id, symbol=BTC, reason="no buy of the bot opened it")
    ]
    assert setup.kill_switch.selling == []
    assert await setup.desk.risk.position(a.id, BTC) is not None


async def test_with_no_fresh_price_a_position_waits_for_one(setup: Setup) -> None:
    a = await setup.bot("btc-a")
    runner = setup.runner([a])
    await setup.buy_signal(runner)
    setup.clock.now += timedelta(seconds=30)  # its price is 30 s old by the kill

    reply = await setup.kill_switch.kill(setup.command(close_positions=True))

    assert reply.closing == [Closing(bot_id=a.id, symbol=BTC, amount=d("9.0768"))]
    await setup.price(runner, bid="105")  # 105 x 0.9995 -> 104.94
    status, filled, _ = await exit_of(setup, "b1-202610221230-x1")
    assert (status, filled) == ("closed", d("9.0768"))
    assert setup.kill_switch.selling == []


# --- The command list and the CLI --------------------------------------------------------


async def test_a_bad_command_is_logged_and_the_next_one_still_runs(setup: Setup) -> None:
    a = await setup.bot("btc-a")
    with capture_logs() as logs:
        async with listening(setup), asyncio.timeout(5):
            await setup.redis.lpush(COMMANDS_KEY, b"not a command")
            reply = await send(setup.redis, setup.command(), reply_within=5)

    assert reply is not None and reply.ok and reply.stopped == [a.id]
    assert [log["error"] for log in logs if log["event"] == "command.invalid"] == [
        "ValidationError"
    ]


REDIS_READ_TIMEOUT = 5  # redis-py's default socket timeout, which the engine's client keeps


async def test_a_listener_idle_longer_than_redis_pys_read_timeout_still_answers_at_once(
    setup: Setup,
) -> None:
    # A blocking pop as long as the socket timeout failed on the client, and the
    # listener backed off for up to a minute: found on the real stack (D43).
    a = await setup.bot("btc-a")
    with capture_logs() as logs:
        async with listening(setup):
            await asyncio.sleep(REDIS_READ_TIMEOUT + 1)
            async with asyncio.timeout(2):
                reply = await send(setup.redis, setup.command(), reply_within=2)

    assert reply is not None and reply.stopped == [a.id]
    assert [log for log in logs if log["event"] == "commands.reconnecting"] == []


async def test_the_sender_can_wait_longer_than_redis_pys_read_timeout(setup: Setup) -> None:
    a = await setup.bot("btc-a")
    waiting = asyncio.create_task(send(setup.redis, setup.command(), reply_within=10))
    await asyncio.sleep(REDIS_READ_TIMEOUT + 1)  # the engine is still starting, say
    assert not waiting.done()

    async with listening(setup), asyncio.timeout(3):
        reply = await waiting

    assert reply is not None and reply.stopped == [a.id]


class BrokenKillSwitch(KillSwitch):
    async def kill(self, command: KillCommand) -> KillReply:
        raise RuntimeError("database down")


async def test_a_kill_that_fails_is_answered_with_its_error(setup: Setup) -> None:
    broken = BrokenKillSwitch(setup.desk.risk, setup.db, setup.market, setup.quotes)
    command = setup.command()
    await setup.redis.lpush(COMMANDS_KEY, command.model_dump_json())

    await CommandListener(setup.redis, broken).drain()

    popped = await setup.redis.blpop([reply_key(command.id)], timeout=1)
    assert popped is not None
    reply = KillReply.model_validate_json(popped[1])
    assert (reply.ok, reply.error) == (False, "RuntimeError: database down")


async def test_the_cli_prints_the_engines_reply(
    setup: Setup, redis_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    a = await setup.bot("btc-a")
    args = argparse.Namespace(close_positions=False, timeout=5.0)
    capsys.readouterr()  # drop the setup's logs
    # The engine's logs captured, so stdout is only what the CLI prints, as in its own process.
    with capture_logs():
        async with listening(setup):
            assert await run_cli(args, redis_url) == 0

    reply = KillReply.model_validate_json(capsys.readouterr().out)
    assert reply.ok and reply.stopped == [a.id]


async def test_the_cli_says_when_the_engine_doesnt_answer(setup: Setup, redis_url: str) -> None:
    args = argparse.Namespace(close_positions=True, timeout=0.2)

    assert await run_cli(args, redis_url) == 1

    # Still queued: the engine carries it out when it starts.
    queued = await setup.redis.lindex(COMMANDS_KEY, 0)
    assert queued is not None
    command = KillCommand.model_validate_json(queued)
    assert (command.close_positions, command.source) == (True, "cli")
