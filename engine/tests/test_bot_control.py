"""The Bots page's commands (control/bots.py, D47; the strategy switch, D51), against a
real TimescaleDB and Redis (see shared.testing), a real PaperDesk, KillSwitch and
BotRunner. The market and the latest price are fakes, with test_kill_switch.py's
numbers: EMA crossover 3/5, 30 flat candles at 100, then a close at 110 is a BUY. With
1000 USDT, all-in, that buys 9.0768 BTC at 110. Slippage 0.05%: a sell at bid B is
limited to B x 0.9995, rounded down.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

import pytest
from redis.asyncio import Redis
from sqlalchemy import URL, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.bots.runner import BotRunner
from engine.bots.store import Bot, load_bot, load_bots, open_orders
from engine.control.bots import BotControl
from engine.control.listener import CommandListener
from engine.exchange.errors import ExchangeError
from engine.exchange.models import Candle, MarketRules, Timeframe
from engine.marketdata.store import CandleStore, Series
from engine.risk.kill import KillSwitch
from engine.risk.paper import PaperDesk
from engine.strategies.base import Position
from shared.commands import (
    COMMANDS_KEY,
    BotAction,
    BotCommand,
    BotReply,
    Closing,
    CreateBotCommand,
    SwitchPolicy,
    SwitchStrategyCommand,
    reply_key,
    send,
)
from shared.db.models import SWITCH_POLICIES, BotRow, OrderRow, RiskEventRow, StrategyConfigRow
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
RSI: dict[str, Any] = {"period": 2, "oversold": 30, "overbought": 70}


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
        if symbol != BTC:
            raise ExchangeError(f"mexc does not have market symbol {symbol}")
        return RULES

    async def watch_candles(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        raise AssertionError("tests feed candles themselves")

    async def close_stream(self, symbol: str, timeframe: Timeframe | None = None) -> None:
        return None


@dataclass
class FakeQuotes:
    clock: Clock
    price: PriceUpdate | None = None

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
        return self.price


async def quiet_stream(symbol: str, timeframe: Timeframe, last: datetime) -> AsyncGenerator[Candle]:
    """A live stream with no candles yet: tests call `on_closed` themselves."""
    await asyncio.Event().wait()
    yield candle(0)  # never reached; makes this a generator


@dataclass
class Setup:
    db: AsyncEngine
    redis: Redis
    clock: Clock
    quotes: FakeQuotes
    market: FakeMarket
    desk: PaperDesk
    kill_switch: KillSwitch
    control: BotControl
    runner: BotRunner | None = None

    def new_runner(self, bots: list[Bot]) -> BotRunner:
        runner = BotRunner(
            bots,
            self.desk.risk,
            self.market,
            self.quotes,
            CandleStore(self.db),
            self.db,
            stream=quiet_stream,
            selling=lambda bot_id: self.kill_switch.sells_for(bot_id),  # as main.py wires it
            clock=self.clock,
        )
        self.runner = runner
        self.kill_switch.runner = runner
        self.control.runner = runner
        return runner

    def create(self, name: str = "btc-ema", **fields: Any) -> CreateBotCommand:
        values: dict[str, Any] = {
            "name": name,
            "strategy": "ema_crossover",
            "params": {"fast": 3, "slow": 5},
            "symbol": BTC,
            "timeframe": "1m",
            "cash": d("1000"),
            **fields,
        }
        return CreateBotCommand(
            **values,
            source="test",
            sent_at=self.clock.now,
            expires_at=self.clock.now + timedelta(seconds=10),
        )

    def command(self, action: BotAction, bot_id: int, *, close: bool = False) -> BotCommand:
        return BotCommand(
            action=action,
            bot_id=bot_id,
            close_position=close,
            source="test",
            sent_at=self.clock.now,
            expires_at=self.clock.now + timedelta(seconds=10),
        )

    def switch(
        self,
        bot_id: int,
        policy: SwitchPolicy,
        strategy: str = "rsi_mean_reversion",
        params: dict[str, Any] | None = None,
    ) -> SwitchStrategyCommand:
        return SwitchStrategyCommand(
            bot_id=bot_id,
            strategy=strategy,
            params=RSI if params is None else params,
            policy=policy,
            source="test",
            sent_at=self.clock.now,
            expires_at=self.clock.now + timedelta(seconds=10),
        )

    async def created(self, name: str = "btc-ema", **fields: Any) -> int:
        reply = await self.control.handle(self.create(name, **fields))
        assert reply.ok and reply.bot_id is not None, reply
        return reply.bot_id

    async def bot_row(self, bot_id: int) -> Any:
        async with self.db.connect() as conn:
            return (await conn.execute(select(BotRow).where(BotRow.id == bot_id))).one()

    async def bought(self, bot_id: int) -> BotRunner:
        """Bot `bot_id`, started, bought 9.0768 BTC at 110 on candle 30."""
        assert (await self.control.handle(self.command("start", bot_id))).ok
        bot, _ = await load_bot(self.db, "mexc", bot_id)
        runner = self.new_runner([bot])
        await runner.warm_up(SERIES)
        self.clock.now += MINUTE
        self.quotes.set(bid="109.9", ask="110")
        await runner.on_closed(SERIES, candle(30, "110"))
        return runner

    async def bought_running(self, runner: BotRunner, bot_id: int) -> None:
        """As `bought`, on `runner` while it runs (inside `running()`), so it can switch."""
        assert (await self.control.handle(self.command("start", bot_id))).ok
        await until(lambda: runner.running == [bot_id])
        await self.closes(runner, 30, "110", bid="109.9")

    async def closes(self, runner: BotRunner, i: int, close: str, *, bid: str) -> None:
        """Candle `i` closes at `close`, 2 s ago, with the market at `bid` / `bid` + 0.1."""
        self.clock.now = T0 + (i + 1) * MINUTE + timedelta(seconds=2)
        self.quotes.set(bid=bid)
        await runner.on_closed(SERIES, candle(i, close))

    async def price(self, runner: BotRunner, *, bid: str, size: str = "50") -> None:
        """A price a second on, as the engine's feed hands it out."""
        self.clock.now += timedelta(seconds=1)
        self.quotes.set(bid=bid, size=size)
        assert self.quotes.price is not None
        await self.desk.on_price(self.quotes.price)
        await runner.on_price(self.quotes.price)
        await self.kill_switch.on_price(self.quotes.price)


@pytest.fixture
async def setup(db_url: URL, redis_url: str) -> AsyncIterator[Setup]:
    db = make_engine(db_url)
    async with db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events, candles RESTART IDENTITY"
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
    control = BotControl(db, desk, market, kill_switch, clock=clock)
    yield Setup(db, redis, clock, quotes, market, desk, kill_switch, control)
    await redis.aclose()
    await db.dispose()


@asynccontextmanager
async def running(runner: BotRunner) -> AsyncIterator[None]:
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0)  # run() under way, so add() works
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():  # noqa: ASYNC110 (waiting on the runner's tasks)
            await asyncio.sleep(0.01)


async def exit_of(setup: Setup, client_order_id: str) -> tuple[str, Decimal, Any]:
    order = await setup.desk.risk.get_order(client_order_id)
    assert order is not None
    exit_ = order.reason["exit"]
    assert isinstance(exit_, dict)
    return order.status, order.filled, exit_["kind"]


# --- Create ------------------------------------------------------------------------------


async def test_create_adds_a_stopped_funded_bot(setup: Setup) -> None:
    command = setup.create(
        sizing={"mode": "percent_of_equity", "percent": "25"},
        stops={"stop_loss": {"kind": "percent", "percent": "3"}},
    )

    reply = await setup.control.handle(command)

    assert reply.ok and (reply.id, reply.status) == (command.id, "stopped")
    assert reply.bot_id is not None
    row = await setup.bot_row(reply.bot_id)
    assert (row.name, row.mode, row.status, row.symbol, row.timeframe) == (
        "btc-ema",
        "paper",
        "stopped",
        BTC,
        "1m",
    )
    assert row.sizing == {"mode": "percent_of_equity", "percent": "25"}
    assert row.stops["stop_loss"] == {"kind": "percent", "percent": "3"}
    async with setup.db.connect() as conn:
        config = (await conn.execute(select(StrategyConfigRow))).one()
    assert (config.strategy, config.version, config.params, config.created_by) == (
        "ema_crossover",
        1,
        {"fast": 3, "slow": 5},
        "test",
    )
    assert (await setup.desk.risk.balances(reply.bot_id))["USDT"].free == d("1000")
    # Stopped: the engine doesn't load it until it's started.
    assert await load_bots(setup.db, "mexc") == []


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"params": {"fast": 9, "slow": 5}}, "fast (9) must be shorter than slow (5)"),
        ({"params": {"fast": 1, "slow": 5}}, "fast: Input should be greater than or equal to 2"),
        ({"params": {"fats": 3}}, "fats: Extra inputs are not permitted"),
        ({"strategy": "moon"}, "unknown strategy 'moon'; known: "),
        ({"timeframe": "2m"}, "timeframe '2m' isn't one of 1m, "),
        ({"symbol": "DOGE/USDT"}, "DOGE/USDT: mexc does not have market symbol DOGE/USDT"),
        ({"symbol": "BTCUSDT"}, "'BTCUSDT' isn't a symbol like BTC/USDT"),
        ({"sizing": {"mode": "fixed"}}, "fixed.amount: Field required"),
        (
            {"stops": {"take_profit": {"kind": "r", "multiple": "2"}}},
            "an R-multiple take-profit needs a stop_loss to measure R by",
        ),
    ],
)
async def test_create_refuses_what_the_engine_cant_run(
    setup: Setup, fields: dict[str, Any], error: str
) -> None:
    reply = await setup.control.handle(setup.create(**fields))

    assert not reply.ok and reply.error is not None
    assert reply.error.startswith(error), reply.error
    async with setup.db.connect() as conn:
        assert (await conn.execute(select(BotRow))).all() == []


async def test_create_refuses_a_name_in_use(setup: Setup) -> None:
    await setup.created()

    reply = await setup.control.handle(setup.create())

    assert (reply.ok, reply.error) == (False, "a bot named 'btc-ema' already exists")


# --- Start and pause ---------------------------------------------------------------------


async def test_start_runs_a_stopped_bot_without_a_restart(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        reply = await setup.control.handle(setup.command("start", bot_id))

        assert (reply.ok, reply.bot_id, reply.status) == (True, bot_id, "running")
        await until(lambda: runner.running == [bot_id])
        # Started twice: still running, once.
        assert (await setup.control.handle(setup.command("start", bot_id))).ok
        assert runner.running == [bot_id]

    row = await setup.bot_row(bot_id)
    assert (row.status, row.stopped_reason, row.paused_reason) == ("running", None, None)


async def test_start_refuses_an_unknown_bot_or_one_it_cant_run(setup: Setup) -> None:
    reply = await setup.control.handle(setup.command("start", 99))
    assert (reply.ok, reply.error) == (False, "there's no bot 99")

    bot_id = await setup.created()
    async with setup.db.begin() as conn:
        await conn.execute(text("UPDATE strategy_configs SET version = 99"))
    reply = await setup.control.handle(setup.command("start", bot_id))
    assert (reply.ok, reply.error) == (
        False,
        "this engine can't run it: ema_crossover is now version 1, not version 99",
    )
    assert (await setup.bot_row(bot_id)).status == "stopped"


async def test_pause_blocks_buys_and_start_resumes(setup: Setup) -> None:
    bot_id = await setup.created()
    stopped = await setup.control.handle(setup.command("pause", bot_id))
    assert (stopped.ok, stopped.error) == (False, "it's stopped: only a running bot can be paused")

    assert (await setup.control.handle(setup.command("start", bot_id))).ok
    reply = await setup.control.handle(setup.command("pause", bot_id))

    assert (reply.ok, reply.status) == (True, "paused")
    row = await setup.bot_row(bot_id)
    assert (row.status, row.paused_reason, row.paused_at) == (
        "paused",
        "paused from test",
        setup.clock.now,
    )
    # Still loaded, as the error breaker's pause (D42): it sells and keeps its stops,
    # but its buy is blocked.
    (bot,) = await load_bots(setup.db, "mexc")
    runner = setup.new_runner([bot])
    await runner.warm_up(SERIES)
    setup.clock.now += MINUTE
    setup.quotes.set(bid="109.9", ask="110")
    await runner.on_closed(SERIES, candle(30, "110"))
    async with setup.db.connect() as conn:
        rules = (await conn.execute(select(RiskEventRow.rule))).scalars().all()
    assert rules == ["bot_paused"]

    assert (await setup.control.handle(setup.command("start", bot_id))).status == "running"
    row = await setup.bot_row(bot_id)
    assert (row.status, row.paused_reason, row.paused_at) == ("running", None, None)


# --- Stop --------------------------------------------------------------------------------


async def test_stop_cancels_its_orders_and_keeps_its_position(setup: Setup) -> None:
    bot_id = await setup.created()
    other = await setup.created("other")
    assert (await setup.control.handle(setup.command("start", other))).ok
    runner = await setup.bought(bot_id)  # 9.0768 at 110
    setup.quotes.set(bid="109.9", ask="110", size="4")
    await runner.on_closed(SERIES, candle(31, "90"))  # SELL: 4 sold, the rest open
    (sell,) = await open_orders(setup.db, bot_id, mode="paper")

    reply = await setup.control.handle(setup.command("stop", bot_id))

    assert (reply.ok, reply.status, reply.canceled) == (True, "stopped", [sell])
    assert (reply.closing, reply.not_closed) == ([], [])
    row = await setup.bot_row(bot_id)
    assert (row.status, row.stopped_reason, row.stopped_at) == (
        "stopped",
        "stopped from test",
        setup.clock.now,
    )
    assert await setup.desk.risk.position(bot_id, BTC) == Position(
        amount=d("5.0768"), avg_entry=d("110")
    )
    # It never acts again: not even to skip the next cross up's BUY (it still holds).
    setup.clock.now += MINUTE
    setup.quotes.set(bid="129.9", ask="130")
    with capture_logs() as logs:
        await runner.on_closed(SERIES, candle(32, "130"))
    assert [log["event"] for log in logs if log.get("bot_id") == bot_id] == []
    assert (await setup.bot_row(other)).status == "running"  # only the one bot


async def test_stop_and_close_sells_until_flat(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = await setup.bought(bot_id)
    setup.quotes.set(bid="105", size="4")

    reply = await setup.control.handle(setup.command("stop", bot_id, close=True))

    # 105 x 0.9995 = 104.9475 -> 104.94; 4 BTC on offer at the bid.
    assert reply.ok and reply.status == "stopped"
    assert reply.closing == [
        Closing(
            bot_id=bot_id,
            symbol=BTC,
            amount=d("9.0768"),
            client_order_id="b1-202610221230-x1",
            status="open",
            filled=d("4"),
        )
    ]
    row = await setup.bot_row(bot_id)
    assert row.stopped_reason == "stopped from test, closing its position"
    assert setup.kill_switch.selling == [(bot_id, BTC)]
    # Not while it's being sold.
    refused = await setup.control.handle(setup.command("start", bot_id))
    assert not refused.ok and "still being sold" in (refused.error or "")

    await setup.price(runner, bid="104")  # replaced at 103.94, and filled

    assert await exit_of(setup, "b1-202610221230-x2") == ("closed", d("5.0768"), "close")
    assert await setup.desk.risk.position(bot_id, BTC) is None
    assert setup.kill_switch.selling == []


async def test_a_restart_carries_on_a_close(setup: Setup) -> None:
    bot_id = await setup.created()
    await setup.bought(bot_id)
    setup.quotes.set(bid="105", size="4")
    await setup.control.handle(setup.command("stop", bot_id, close=True))  # -x1 waits

    # A new desk and kill switch on the same database, as after a crash.
    setup.desk = PaperDesk(setup.db, setup.quotes, setup.market, clock=setup.clock)
    setup.kill_switch = KillSwitch(
        setup.desk.risk, setup.db, setup.market, setup.quotes, clock=setup.clock
    )
    runner = setup.new_runner(await load_bots(setup.db, "mexc"))
    await runner.recover()  # cancels -x1
    assert await setup.kill_switch.resume() == [(bot_id, BTC)]
    await setup.price(runner, bid="104.2")

    assert await exit_of(setup, "b1-202610221230-x2") == ("closed", d("5.0768"), "close")


# --- Switch (P6-1, D51) -------------------------------------------------------------------
#
# Bought at 110 by EMA 3/5 (config 1), then switched to RSI(2) 30/70 (config 2). On
# candle 31, flat at 110, RSI is still 100 (only gains since the jump), so RSI says SELL
# and EMA holds. On candle 32 at 90, EMA crosses down: SELL. On candle 33 at 100, RSI
# rises from 11.1 back through 30 to 52.9: BUY.


async def orders(setup: Setup, bot_id: int) -> list[tuple[str, str, Decimal, Any]]:
    """The bot's orders, oldest first: client order ID, status, filled, and the config
    whose signal placed it (None for an exit)."""
    async with setup.db.connect() as conn:
        rows = (
            await conn.execute(
                select(OrderRow.client_order_id, OrderRow.status, OrderRow.filled, OrderRow.reason)
                .where(OrderRow.bot_id == bot_id)
                .order_by(OrderRow.id)
            )
        ).all()
    return [
        (
            r.client_order_id,
            r.status,
            r.filled,
            r.reason["strategy_config"]["id"] if "signal" in r.reason else None,
        )
        for r in rows
    ]


BUY = ("b1-202610221230-b", "closed", d("9.0768"), 1)


def test_switch_policies_match_the_database_check() -> None:
    assert get_args(SwitchPolicy) == SWITCH_POLICIES


async def test_hand_over_lets_the_new_config_sell_the_position(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)

        reply = await setup.control.handle(setup.switch(bot_id, "hand_over"))

        assert (reply.ok, reply.status, reply.strategy_config_id) == (True, "running", 2)
        assert (reply.closing, reply.not_closed) == ([], [])
        await until(lambda: runner.switching == [])
        with capture_logs() as logs:
            await setup.closes(runner, 31, "110", bid="109.9")

    assert [log["event"] for log in logs if log["event"].startswith("bot.switch")] == [
        "bot.switched"
    ]
    # RSI's SELL, at the bid: the whole position, in the config the order names.
    assert await orders(setup, bot_id) == [
        BUY,
        ("b1-202610221231-s", "closed", d("9.0768"), 2),
    ]
    assert await setup.desk.risk.position(bot_id, BTC) is None
    row = await setup.bot_row(bot_id)
    assert (row.active_strategy_config_id, row.switch_policy) == (2, "hand_over")
    async with setup.db.connect() as conn:
        config = (
            await conn.execute(select(StrategyConfigRow).where(StrategyConfigRow.id == 2))
        ).one()
    assert (config.bot_id, config.strategy, config.version, config.created_by) == (
        bot_id,
        "rsi_mean_reversion",
        1,
        "test",
    )
    assert config.params == {"period": 2, "oversold": 30.0, "overbought": 70.0}


async def test_hand_over_keeps_the_trades_stops(setup: Setup) -> None:
    bot_id = await setup.created(stops={"stop_loss": {"kind": "percent", "percent": "3"}})
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)  # stop at 110 x 0.97 = 106.7
        assert (await setup.control.handle(setup.switch(bot_id, "hand_over"))).ok
        await until(lambda: runner.switching == [])
        await setup.closes(runner, 31, "107", bid="106.9")  # RSI: 62.5, so no SELL

        await setup.price(runner, bid="106.5")

    assert await exit_of(setup, "b1-202610221230-x1") == ("closed", d("9.0768"), "stop_loss")
    assert await setup.desk.risk.position(bot_id, BTC) is None


async def test_keep_leaves_the_position_to_the_config_that_bought_it(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)

        reply = await setup.control.handle(setup.switch(bot_id, "keep"))

        assert (reply.ok, reply.strategy_config_id) == (True, 2)
        await until(lambda: runner.switching == [])
        with capture_logs() as logs:
            await setup.closes(runner, 31, "110", bid="109.9")  # RSI: SELL; EMA: hold
        (skipped,) = [log for log in logs if log["event"] == "bot.skipped"]
        assert skipped["reason"] == (
            "its position is strategy config 1's to sell (switched with keep)"
        )
        assert await orders(setup, bot_id) == [BUY]
        assert await setup.desk.risk.position(bot_id, BTC) == Position(
            amount=d("9.0768"), avg_entry=d("110")
        )
        # A restart now would carry on the same way.
        (loaded,) = await load_bots(setup.db, "mexc")
        assert loaded.strategy_config_id == 2
        assert loaded.keeper is not None and loaded.keeper.strategy_config_id == 1

        await setup.closes(runner, 32, "90", bid="89.9")  # EMA's SELL, as config 1's
        with capture_logs() as logs:
            await setup.closes(runner, 33, "100", bid="99.9")  # flat: RSI's BUY

    assert [log["event"] for log in logs if log["event"] == "bot.keeper_done"] == [
        "bot.keeper_done"
    ]
    (*_, sell, buy) = await orders(setup, bot_id)
    assert sell == ("b1-202610221232-s", "closed", d("9.0768"), 1)
    assert (buy[0], buy[1], buy[3]) == ("b1-202610221233-b", "closed", 2)
    (loaded,) = await load_bots(setup.db, "mexc")
    assert loaded.keeper is None  # its own buy now


async def test_a_keeper_that_no_longer_builds_hands_the_position_over(setup: Setup) -> None:
    bot_id = await setup.created()
    await setup.bought(bot_id)
    setup.control.runner = None  # only the database: this is about loading it
    assert (await setup.control.handle(setup.switch(bot_id, "keep"))).ok
    # As if EMA crossover had moved on to version 2 since the buy.
    async with setup.db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE orders SET reason = jsonb_set(reason, '{strategy_config,version}', '99')"
                " WHERE client_order_id = 'b1-202610221230-b'"
            )
        )

    with capture_logs() as logs:
        (loaded,) = await load_bots(setup.db, "mexc")

    assert (loaded.strategy_config_id, loaded.keeper) == (2, None)
    (failed,) = [log for log in logs if log["event"] == "bot.keep_failed"]
    assert failed["handed_over_to"] == 2
    assert failed["reason"] == "ema_crossover is now version 1, not version 99"


async def test_close_first_sells_the_position_before_the_new_config_trades(
    setup: Setup,
) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)
        setup.quotes.set(bid="105", size="4")

        reply = await setup.control.handle(setup.switch(bot_id, "close_first"))

        # 105 x 0.9995 = 104.9475 -> 104.94; 4 BTC on offer at the bid.
        assert (reply.ok, reply.status, reply.strategy_config_id) == (True, "running", 2)
        assert reply.closing == [
            Closing(
                bot_id=bot_id,
                symbol=BTC,
                amount=d("9.0768"),
                client_order_id="b1-202610221230-x1",
                status="open",
                filled=d("4"),
            )
        ]
        assert await exit_of(setup, "b1-202610221230-x1") == ("open", d("4"), "switch")
        assert setup.kill_switch.selling == [(bot_id, BTC)]
        refused = await setup.control.handle(setup.switch(bot_id, "keep", params={"period": 3}))
        assert not refused.ok and "still being sold" in (refused.error or "")
        await until(lambda: runner.switching == [])

        # Its candle: the new config takes over, but only takes the candle in; the exit
        # stays (no leftovers cancelled), and RSI's SELL isn't placed.
        with capture_logs() as logs:
            await setup.closes(runner, 31, "110", bid="105")
        assert [log["event"] for log in logs if log.get("bot_id") == bot_id] == [
            "bot.switched",
            "bot.waiting",
        ]
        assert await exit_of(setup, "b1-202610221230-x1") == ("open", d("4"), "switch")

        await setup.price(runner, bid="104")  # replaced at 103.94, and filled

        assert await exit_of(setup, "b1-202610221230-x2") == ("closed", d("5.0768"), "switch")
        assert await setup.desk.risk.position(bot_id, BTC) is None
        assert setup.kill_switch.selling == []
        assert (await setup.bot_row(bot_id)).status == "running"  # unlike stop + close

        with capture_logs() as logs:
            await setup.closes(runner, 32, "110", bid="109.9")
        assert "bot.waiting" not in [log["event"] for log in logs]
    assert [o[0] for o in await orders(setup, bot_id)] == [
        "b1-202610221230-b",
        "b1-202610221230-x1",
        "b1-202610221230-x2",
    ]


async def test_a_restart_carries_on_a_close_first(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)
        setup.quotes.set(bid="105", size="4")
        assert (await setup.control.handle(setup.switch(bot_id, "close_first"))).ok

    # A new desk and kill switch on the same database, as after a crash.
    setup.desk = PaperDesk(setup.db, setup.quotes, setup.market, clock=setup.clock)
    setup.kill_switch = KillSwitch(
        setup.desk.risk, setup.db, setup.market, setup.quotes, clock=setup.clock
    )
    (bot,) = await load_bots(setup.db, "mexc")
    assert (bot.strategy.name, bot.keeper) == ("rsi_mean_reversion", None)
    runner = setup.new_runner([bot])
    await runner.recover()  # cancels -x1
    assert await setup.kill_switch.resume() == [(bot_id, BTC)]
    await runner.warm_up(SERIES)
    with capture_logs() as logs:
        await setup.closes(runner, 31, "110", bid="105")  # held: still selling
    assert [log["event"] for log in logs if log.get("bot_id") == bot_id] == ["bot.waiting"]

    await setup.price(runner, bid="104.2")

    assert await exit_of(setup, "b1-202610221230-x2") == ("closed", d("5.0768"), "switch")


async def test_a_new_policy_alone_adds_no_config(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)
        assert (await setup.control.handle(setup.switch(bot_id, "keep"))).ok
        assert (await load_bots(setup.db, "mexc"))[0].keeper is not None

        reply = await setup.control.handle(
            setup.switch(bot_id, "hand_over", strategy="rsi_mean_reversion")
        )

        assert (reply.ok, reply.strategy_config_id, reply.closing) == (True, None, [])
        # Its trade is now the bot's config's to sell, in memory as after a restart.
        (loaded,) = await load_bots(setup.db, "mexc")
        assert (loaded.strategy_config_id, loaded.keeper) == (2, None)
        await until(lambda: runner.switching == [])
        await setup.closes(runner, 31, "110", bid="109.9")  # RSI's SELL, acted on

    assert (await orders(setup, bot_id))[-1] == ("b1-202610221231-s", "closed", d("9.0768"), 2)
    row = await setup.bot_row(bot_id)
    assert (row.active_strategy_config_id, row.switch_policy) == (2, "hand_over")


async def test_a_stopped_bot_switches_in_the_database_and_starts_with_it(setup: Setup) -> None:
    bot_id = await setup.created()

    reply = await setup.control.handle(setup.switch(bot_id, "close_first"))

    assert (reply.ok, reply.status, reply.strategy_config_id) == (True, "stopped", 2)
    assert (reply.closing, reply.not_closed) == ([], [])  # flat: nothing to close
    bot, status = await load_bot(setup.db, "mexc", bot_id)
    assert (bot.strategy.name, bot.strategy_config_id, status) == (
        "rsi_mean_reversion",
        2,
        "stopped",
    )


@pytest.mark.parametrize(
    ("bot_id", "strategy", "params", "policy", "error"),
    [
        (99, "rsi_mean_reversion", RSI, "keep", "there's no bot 99"),
        (
            1,
            "rsi_mean_reversion",
            {"period": 1},
            "keep",
            "period: Input should be greater than or equal to 2",
        ),
        (1, "moon", {}, "keep", "unknown strategy 'moon'; known: "),
        (
            1,
            "ema_crossover",
            {"fast": 3, "slow": 5},
            "keep",
            "nothing to change: it already runs that strategy with those params, and its "
            "switch policy is keep",
        ),
    ],
)
async def test_switch_refuses_what_it_cant_do(
    setup: Setup,
    bot_id: int,
    strategy: str,
    params: dict[str, Any],
    policy: SwitchPolicy,
    error: str,
) -> None:
    await setup.created()

    reply = await setup.control.handle(setup.switch(bot_id, policy, strategy, params))

    assert not reply.ok and reply.error is not None
    assert reply.error.startswith(error), reply.error
    async with setup.db.connect() as conn:
        assert len((await conn.execute(select(StrategyConfigRow))).all()) == 1


# --- Over Redis ---------------------------------------------------------------------------


@asynccontextmanager
async def listening(setup: Setup) -> AsyncIterator[None]:
    listener = CommandListener(setup.redis, setup.kill_switch, setup.control, clock=setup.clock)
    task = asyncio.create_task(listener.run())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_commands_over_redis_get_their_replies(setup: Setup) -> None:
    async with listening(setup), asyncio.timeout(5):
        created = await send(setup.redis, setup.create(), reply_within=5)
        assert created is not None and created.ok and created.bot_id is not None
        started = await send(setup.redis, setup.command("start", created.bot_id), reply_within=5)
        refused = await send(setup.redis, setup.create(), reply_within=5)
        switched = await send(setup.redis, setup.switch(created.bot_id, "keep"), reply_within=5)

    assert started is not None and (started.ok, started.status) == (True, "running")
    assert refused is not None and refused.error == "a bot named 'btc-ema' already exists"
    assert switched is not None and (switched.ok, switched.strategy_config_id) == (True, 2)


async def test_an_expired_command_is_dropped_unanswered(setup: Setup) -> None:
    bot_id = await setup.created()
    command = setup.command("start", bot_id)
    await setup.redis.lpush(COMMANDS_KEY, command.model_dump_json())
    setup.clock.now += timedelta(seconds=10)  # the sender gave up waiting

    with capture_logs() as logs:
        listener = CommandListener(setup.redis, setup.kill_switch, setup.control, clock=setup.clock)
        assert await listener.drain() == 1

    assert (await setup.bot_row(bot_id)).status == "stopped"
    assert await setup.redis.exists(reply_key(command.id)) == 0
    (expired,) = [log for log in logs if log["event"] == "command.expired"]
    assert (expired["id"], expired["age_s"]) == (str(command.id), 10.0)


async def test_a_start_sent_while_the_engine_was_down_applies_before_bots_load(
    setup: Setup,
) -> None:
    bot_id = await setup.created()
    command = setup.command("start", bot_id)
    await setup.redis.lpush(COMMANDS_KEY, command.model_dump_json())

    # The engine at startup: no runner yet.
    listener = CommandListener(setup.redis, setup.kill_switch, setup.control, clock=setup.clock)
    assert await listener.drain() == 1

    assert [b.id for b in await load_bots(setup.db, "mexc")] == [bot_id]
    popped = await setup.redis.blpop([reply_key(command.id)], timeout=1)
    assert popped is not None and BotReply.model_validate_json(popped[1]).ok


async def test_a_listener_without_bot_control_answers_with_an_error(setup: Setup) -> None:
    command = setup.command("start", 1)
    await setup.redis.lpush(COMMANDS_KEY, command.model_dump_json())

    await CommandListener(setup.redis, setup.kill_switch, clock=setup.clock).drain()

    popped = await setup.redis.blpop([reply_key(command.id)], timeout=1)
    assert popped is not None
    reply = BotReply.model_validate_json(popped[1])
    assert (reply.ok, reply.error) == (False, "RuntimeError: this engine takes no bot commands")
