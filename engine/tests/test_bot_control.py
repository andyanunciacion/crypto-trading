"""The Bots page's commands (control/bots.py, D47; the strategy switch, D51), against a
real TimescaleDB and Redis (see shared.testing), a real PaperDesk, KillSwitch and
BotRunner. The market and the latest price are fakes, with test_kill_switch.py's
numbers: EMA crossover 3/5, 30 flat candles at 100, then a close at 110 is a BUY. With
1000 USDT, all-in, that buys 9.0768 BTC at 110. Slippage 0.05%: a sell at bid B is
limited to B x 0.9995, rounded down.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

import pytest
from redis.asyncio import Redis
from sqlalchemy import URL, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.bots.runner import BotRunner, RegimeHook
from engine.bots.store import Bot, load_bot, load_bots, open_orders
from engine.control.bots import BotControl
from engine.control.listener import CommandListener
from engine.exchange.errors import ExchangeError
from engine.exchange.models import Candle, MarketRules, Timeframe
from engine.marketdata.store import CandleStore, Series
from engine.regime import switcher
from engine.regime.detector import Regime, RegimeDetector, RegimeParams, RegimeReading
from engine.regime.switcher import RegimeCheck
from engine.risk.kill import KillSwitch
from engine.risk.paper import PaperDesk
from engine.strategies.base import Position
from shared.commands import (
    COMMANDS_KEY,
    AutoSwitchCommand,
    BotAction,
    BotCommand,
    BotReply,
    Closing,
    CreateBotCommand,
    RollbackCommand,
    SwitchPolicy,
    SwitchStrategyCommand,
    reply_key,
    send,
)
from shared.db.models import (
    SWITCH_POLICIES,
    BotRow,
    OrderRow,
    RiskEventRow,
    StrategyConfigRow,
    StrategySwitchRow,
)
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

    def new_runner(self, bots: list[Bot], *, on_regime: RegimeHook | None = None) -> BotRunner:
        runner = BotRunner(
            bots,
            self.desk.risk,
            self.market,
            self.quotes,
            CandleStore(self.db),
            self.db,
            stream=quiet_stream,
            # As main.py wires them.
            selling=lambda bot_id: self.kill_switch.sells_for(bot_id),
            on_regime=self.control.auto_switch if on_regime is None else on_regime,
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

    def auto(
        self,
        bot_id: int,
        *,
        min_hold: int = 3,
        policy: SwitchPolicy = "keep",
        off: bool = False,
        **mapping: dict[str, Any],
    ) -> AutoSwitchCommand:
        """Auto-switching on for `bot_id` (trend: EMA 3/5, range: RSI(2), else stay flat;
        `mapping` overrides a regime), or off."""
        settings: dict[str, Any] = {
            "mapping": {
                "trending_up": {"strategy": "ema_crossover", "params": {"fast": 3, "slow": 5}},
                "ranging": {"strategy": "rsi_mean_reversion", "params": RSI},
                "trending_down": {"strategy": "stay_flat"},
                "high_volatility": {"strategy": "stay_flat"},
                **mapping,
            },
            "min_hold": min_hold,
            "policy": policy,
        }
        return AutoSwitchCommand(
            bot_id=bot_id,
            settings=None if off else settings,
            source="test",
            sent_at=self.clock.now,
            expires_at=self.clock.now + timedelta(seconds=10),
        )

    def rollback(self, bot_id: int, config_id: int, policy: SwitchPolicy) -> RollbackCommand:
        return RollbackCommand(
            bot_id=bot_id,
            config_id=config_id,
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


async def point_at_config(
    db: AsyncEngine,
    bot_id: int,
    *,
    strategy: str = "ema_crossover",
    version: int = 1,
    params: dict[str, Any] | None = None,
    active: bool = True,
) -> int:
    """Add a config to bot `bot_id` as no command would (an old version, say), and make
    it the bot's active one unless `active` is False. Its id."""
    async with db.begin() as conn:
        config_id: int = (
            await conn.execute(
                insert(StrategyConfigRow)
                .values(
                    bot_id=bot_id,
                    strategy=strategy,
                    version=version,
                    params={"fast": 3, "slow": 5} if params is None else params,
                    created_by="test",
                )
                .returning(StrategyConfigRow.id)
            )
        ).scalar_one()
        if active:
            await conn.execute(
                update(BotRow)
                .where(BotRow.id == bot_id)
                .values(active_strategy_config_id=config_id)
            )
    return config_id


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
    await point_at_config(setup.db, bot_id, version=99)  # configs never change (D52)
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
        amount=d("5.0768"), avg_entry=d("110"), last_entry=d("110")
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
            amount=d("9.0768"), avg_entry=d("110"), last_entry=d("110")
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


# --- Rollback (P6-2, D52) -------------------------------------------------------------


async def configs(setup: Setup) -> list[tuple[int, int, str, dict[str, Any], int | None]]:
    """Every strategy config: id, bot, strategy, params, and what it restores."""
    async with setup.db.connect() as conn:
        rows = (await conn.execute(select(StrategyConfigRow).order_by(StrategyConfigRow.id))).all()
    return [(r.id, r.bot_id, r.strategy, r.params, r.restored_from) for r in rows]


EMA_3_5 = {"fast": 3, "slow": 5}
RSI_2 = {"period": 2, "oversold": 30.0, "overbought": 70.0}


async def test_a_rollback_restores_the_earlier_params_and_keeps_both_in_history(
    setup: Setup,
) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        assert (await setup.control.handle(setup.command("start", bot_id))).ok
        await until(lambda: runner.running == [bot_id])
        assert (await setup.control.handle(setup.switch(bot_id, "hand_over"))).ok
        await until(lambda: runner.switching == [])
        await setup.closes(runner, 30, "100", bid="99.9")  # RSI takes over: 50, so it holds

        with capture_logs() as logs:
            reply = await setup.control.handle(setup.rollback(bot_id, 1, "hand_over"))

        assert (reply.ok, reply.status, reply.strategy_config_id) == (True, "running", 3)
        (switch,) = [log for log in logs if log["event"] == "bot.switch"]
        assert (switch["old"]["id"], switch["new"]["id"], switch["restored_from"]) == (2, 3, 1)
        assert switch["switching"] is True
        await until(lambda: runner.switching == [])
        await setup.closes(runner, 31, "110", bid="109.9")  # EMA 3/5's BUY, as config 3

    assert await configs(setup) == [
        (1, bot_id, "ema_crossover", EMA_3_5, None),
        (2, bot_id, "rsi_mean_reversion", RSI_2, None),
        (3, bot_id, "ema_crossover", EMA_3_5, 1),
    ]
    assert (await setup.bot_row(bot_id)).active_strategy_config_id == 3
    ((client_order_id, status, _, config_id),) = await orders(setup, bot_id)
    assert (client_order_id, status, config_id) == ("b1-202610221231-b", "closed", 3)


async def test_rolling_back_to_the_buyers_params_ends_its_keep(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)  # bought by config 1
        assert (await setup.control.handle(setup.switch(bot_id, "keep"))).ok
        assert (await load_bots(setup.db, "mexc"))[0].keeper is not None

        reply = await setup.control.handle(setup.rollback(bot_id, 1, "keep"))

        assert (reply.ok, reply.strategy_config_id, reply.closing) == (True, 3, [])
        # Config 3 is the trade's buyer's strategy and params: no keeper, after a restart too.
        (loaded,) = await load_bots(setup.db, "mexc")
        assert (loaded.strategy_config_id, loaded.keeper) == (3, None)
        await until(lambda: runner.switching == [])
        await setup.closes(runner, 31, "110", bid="109.9")
        await setup.closes(runner, 32, "90", bid="89.9")  # EMA's SELL, as config 3's

    assert await orders(setup, bot_id) == [
        BUY,
        ("b1-202610221232-s", "closed", d("9.0768"), 3),
    ]


async def test_a_rollback_with_close_first_sells_the_position(setup: Setup) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)
        assert (await setup.control.handle(setup.switch(bot_id, "hand_over"))).ok
        await until(lambda: runner.switching == [])
        setup.quotes.set(bid="105", size="4")

        reply = await setup.control.handle(setup.rollback(bot_id, 1, "close_first"))

        assert (reply.ok, reply.strategy_config_id) == (True, 3)
        assert [c.client_order_id for c in reply.closing] == ["b1-202610221230-x1"]
        assert await exit_of(setup, "b1-202610221230-x1") == ("open", d("4"), "switch")
        refused = await setup.control.handle(setup.rollback(bot_id, 2, "keep"))
        assert refused.error is not None and "still being sold" in refused.error, refused

    assert (await setup.bot_row(bot_id)).switch_policy == "close_first"


async def test_a_stopped_bot_rolls_back_in_the_database(setup: Setup) -> None:
    bot_id = await setup.created()
    assert (await setup.control.handle(setup.switch(bot_id, "keep"))).ok

    reply = await setup.control.handle(setup.rollback(bot_id, 1, "keep"))

    assert (reply.ok, reply.status, reply.strategy_config_id) == (True, "stopped", 3)
    bot, status = await load_bot(setup.db, "mexc", bot_id)
    assert (bot.strategy.name, bot.strategy.params.model_dump(mode="json"), status) == (
        "ema_crossover",
        EMA_3_5,
        "stopped",
    )
    assert bot.strategy_config_id == 3


@pytest.mark.parametrize(
    ("bot_id", "config_id", "error"),
    [
        (99, 1, "bot 99 has no strategy config 1"),
        (1, 3, "bot 1 has no strategy config 3"),  # bot 2's
        (1, 9, "bot 1 has no strategy config 9"),
        (1, 2, "nothing to change: it already runs that config's strategy and params"),
        (1, 4, "can't roll back to config 4: ema_crossover is now version 1, not version 0"),
    ],
)
async def test_rollback_refuses_what_it_cant_do(
    setup: Setup, bot_id: int, config_id: int, error: str
) -> None:
    await setup.created()  # bot 1, config 1
    assert (await setup.control.handle(setup.switch(1, "keep"))).ok  # config 2
    await setup.created("other")  # bot 2, config 3
    await point_at_config(setup.db, 1, version=0, active=False)  # config 4: an old version
    before = await configs(setup)

    reply = await setup.control.handle(setup.rollback(bot_id, config_id, "hand_over"))

    assert (reply.ok, reply.error) == (False, error)
    assert await configs(setup) == before
    assert (await setup.bot_row(1)).switch_policy == "keep"


# --- Auto-switching (P6-4, D54) ----------------------------------------------------------
#
# The regime detector is scripted: each candle reads as `Regimes` says (trending up
# unless told otherwise), after a 2-candle warm-up, so a test says exactly when a regime
# starts. The detector's own rules are test_regime.py's; the rule on readings is
# test_regime_switcher.py's. These check that the engine acts on it.


@dataclass
class Regimes:
    """The regime of each candle, by index: `start[i]` from candle i on."""

    start: dict[int, Regime] = field(default_factory=dict)

    def at(self, index: int) -> Regime:
        earlier = [i for i in self.start if i <= index]
        return self.start[max(earlier)] if earlier else "trending_up"


class ScriptedDetector(RegimeDetector):
    def __init__(self, regimes: Regimes) -> None:
        super().__init__(RegimeParams())
        self.regimes = regimes

    @property
    def warmup_candles(self) -> int:
        return 2

    def detect(self, candles: Sequence[Candle]) -> RegimeReading:
        ts = candles[-1].ts
        if len(candles) < self.warmup_candles:
            return RegimeReading(regime="unclear", ts=ts, reason="warming up")
        regime = self.regimes.at((ts - T0) // MINUTE)
        return RegimeReading(regime=regime, ts=ts, reason=f"scripted {regime}", indicators={})


@pytest.fixture
def regimes(monkeypatch: pytest.MonkeyPatch) -> Regimes:
    regimes = Regimes()
    monkeypatch.setattr(switcher, "RegimeDetector", lambda params: ScriptedDetector(regimes))
    return regimes


async def switches(setup: Setup) -> list[Any]:
    async with setup.db.connect() as conn:
        query = select(StrategySwitchRow).order_by(StrategySwitchRow.id)
        return list((await conn.execute(query)).all())


async def test_the_regime_switches_a_bot_only_after_the_minimum_hold(
    setup: Setup, regimes: Regimes
) -> None:
    regimes.start[30] = "ranging"
    bot_id = await setup.created()  # EMA 3/5: what the trend maps to
    assert (await setup.control.handle(setup.auto(bot_id, min_hold=3))).ok
    runner = setup.new_runner([])
    async with running(runner):
        assert (await setup.control.handle(setup.command("start", bot_id))).ok
        await until(lambda: runner.running == [bot_id])

        with capture_logs() as logs:
            await setup.closes(runner, 30, "100", bid="99.9")
            await setup.closes(runner, 31, "100", bid="99.9")
        readings = [(log["regime"], log["held"]) for log in logs if log["event"] == "bot.regime"]
        assert readings == [("ranging", 1), ("ranging", 2)]
        assert runner.switching == []
        assert await switches(setup) == []

        await setup.closes(runner, 32, "100", bid="99.9")  # held 3: the switch
        assert runner.switching == [bot_id]
        await until(lambda: runner.switching == [])
        with capture_logs() as logs:
            await setup.closes(runner, 33, "100", bid="99.9")

    (switched,) = [log for log in logs if log["event"] == "bot.switched"]
    assert (switched["old"]["id"], switched["new"]["id"]) == (1, 2)
    assert switched["new"]["strategy"] == "rsi_mean_reversion"
    (row,) = await switches(setup)
    assert (row.bot_id, row.from_config, row.to_config, row.trigger, row.policy) == (
        bot_id,
        1,
        2,
        "regime",
        "keep",
    )
    assert (row.regime, row.candle_ts, row.source) == ("ranging", T0 + 32 * MINUTE, "regime")
    assert row.reason == (
        "ranging for 3 candles in a row (scripted ranging): switch to rsi_mean_reversion v1 "
        '{"period": 2, "oversold": 30.0, "overbought": 70.0}'
    )
    assert (row.reading["regime"], row.reading["held"], row.reading["min_hold"]) == (
        "ranging",
        3,
        3,
    )
    (config,) = [c for c in await configs(setup) if c[0] == 2]
    assert config == (2, bot_id, "rsi_mean_reversion", RSI_2, None)
    bot_row = await setup.bot_row(bot_id)
    assert (bot_row.active_strategy_config_id, bot_row.switch_policy) == (2, "keep")


async def test_a_crash_with_hand_over_sells_the_position_through_stay_flat(
    setup: Setup, regimes: Regimes
) -> None:
    regimes.start[31] = "high_volatility"
    bot_id = await setup.created()
    assert (await setup.control.handle(setup.auto(bot_id, min_hold=2, policy="hand_over"))).ok
    runner = setup.new_runner([])
    async with running(runner):
        await setup.bought_running(runner, bot_id)  # candle 30, still trending up
        await setup.closes(runner, 31, "110", bid="109.9")
        await setup.closes(runner, 32, "110", bid="109.9")  # held 2: to stay_flat
        await until(lambda: runner.switching == [])
        await setup.closes(runner, 33, "110", bid="109.9")  # stay_flat's SELL

    # Sold whole, at the bid, in the config the order names: the regime's.
    assert await orders(setup, bot_id) == [BUY, ("b1-202610221233-s", "closed", d("9.0768"), 2)]
    assert await setup.desk.risk.position(bot_id, BTC) is None
    (row,) = await switches(setup)
    assert (row.regime, row.policy, row.to_config) == ("high_volatility", "hand_over", 2)


async def test_a_restart_mid_hold_neither_switches_early_nor_starts_over(
    setup: Setup, regimes: Regimes
) -> None:
    regimes.start[30] = "ranging"
    bot_id = await setup.created()
    assert (await setup.control.handle(setup.auto(bot_id, min_hold=3))).ok
    runner = setup.new_runner([])
    async with running(runner):
        assert (await setup.control.handle(setup.command("start", bot_id))).ok
        await until(lambda: runner.running == [bot_id])
        await setup.closes(runner, 30, "100", bid="99.9")
        await setup.closes(runner, 31, "100", bid="99.9")  # held 2

    # A restart: REST now has candles 30 and 31, and the new runner warms up from it.
    setup.market.rest += [candle(30), candle(31)]
    (bot,) = await load_bots(setup.db, "mexc")
    assert bot.auto is not None
    runner = setup.new_runner([bot])
    async with running(runner):
        await until(lambda: runner.running == [bot_id])
        assert await switches(setup) == []
        await setup.closes(runner, 32, "100", bid="99.9")  # held 3, counted from the candles
        await until(lambda: runner.switching == [])

    (row,) = await switches(setup)
    assert (row.regime, row.candle_ts) == ("ranging", T0 + 32 * MINUTE)


async def test_one_switch_at_a_time(setup: Setup, regimes: Regimes) -> None:
    regimes.start[30] = "ranging"
    calls: list[datetime] = []
    gate = asyncio.Event()

    async def slow(bot: Bot, check: RegimeCheck) -> None:
        calls.append(check.reading.ts)
        await gate.wait()

    bot_id = await setup.created()
    assert (await setup.control.handle(setup.auto(bot_id, min_hold=1))).ok
    runner = setup.new_runner([], on_regime=slow)
    async with running(runner):
        assert (await setup.control.handle(setup.command("start", bot_id))).ok
        await until(lambda: runner.running == [bot_id])
        await setup.closes(runner, 30, "100", bid="99.9")
        await setup.closes(runner, 31, "100", bid="99.9")  # the first is still being made
        assert calls == [T0 + 30 * MINUTE]

        gate.set()
        await until(lambda: runner.switching == [])
        await setup.closes(runner, 32, "100", bid="99.9")  # it made none: asked again
        await until(lambda: runner.switching == [])

    assert calls == [T0 + 30 * MINUTE, T0 + 32 * MINUTE]


async def test_a_manual_switch_is_refused_while_auto_switching_is_on(setup: Setup) -> None:
    bot_id = await setup.created()
    assert (await setup.control.handle(setup.auto(bot_id))).ok

    switch = await setup.control.handle(setup.switch(bot_id, "keep"))
    rollback = await setup.control.handle(setup.rollback(bot_id, 1, "keep"))

    refusal = (
        "auto-switching is on, and the regime would switch it back: turn auto-switching off first"
    )
    assert (switch.ok, switch.error) == (False, refusal)
    assert (rollback.ok, rollback.error) == (False, refusal)
    assert len(await configs(setup)) == 1

    # Off again, both work, and each is recorded.
    assert (await setup.control.handle(setup.auto(bot_id, off=True))).ok
    assert (await setup.control.handle(setup.switch(bot_id, "keep"))).ok
    assert (await setup.control.handle(setup.rollback(bot_id, 1, "hand_over"))).ok
    assert [
        (r.from_config, r.to_config, r.trigger, r.policy, r.regime, r.reason, r.source)
        for r in await switches(setup)
    ] == [
        (1, 2, "manual", "keep", None, "switched by hand (test)", "test"),
        (2, 3, "rollback", "hand_over", None, "rolled back by hand (test)", "test"),
    ]


async def test_turning_auto_switching_on_reloads_a_running_bot(
    setup: Setup, regimes: Regimes
) -> None:
    bot_id = await setup.created()
    runner = setup.new_runner([])
    async with running(runner):
        assert (await setup.control.handle(setup.command("start", bot_id))).ok
        await until(lambda: runner.running == [bot_id])
        with capture_logs() as logs:
            await setup.closes(runner, 30, "100", bid="99.9")
        assert "bot.regime" not in [log["event"] for log in logs]

        with capture_logs() as logs:
            # A long hold: the readings are still building up, so logged at INFO.
            reply = await setup.control.handle(setup.auto(bot_id, min_hold=40))
        assert (reply.ok, reply.status, reply.strategy_config_id) == (True, "running", None)
        (set_,) = [log for log in logs if log["event"] == "bot.auto_switch_set"]
        assert set_["reloading"] is True
        assert set_["settings"]["mapping"]["ranging"] == {
            "strategy": "rsi_mean_reversion",
            "params": RSI_2,
            "version": 1,  # stored with the strategy's version
        }
        await until(lambda: runner.switching == [])
        with capture_logs() as logs:
            await setup.closes(runner, 31, "100", bid="99.9")
        assert [log["regime"] for log in logs if log["event"] == "bot.regime"] == ["trending_up"]

        assert (await setup.control.handle(setup.auto(bot_id, off=True))).ok
        await until(lambda: runner.switching == [])
        with capture_logs() as logs:
            await setup.closes(runner, 32, "100", bid="99.9")
        assert "bot.regime" not in [log["event"] for log in logs]

    assert len(await configs(setup)) == 1  # no config added either way
    assert (await setup.bot_row(bot_id)).auto_switch is None


@pytest.mark.parametrize(
    ("bot_id", "mapping", "min_hold", "error"),
    [
        (99, {}, 3, "there's no bot 99"),
        (
            1,
            {"ranging": {"strategy": "rsi_mean_reversion", "params": {"period": 1}}},
            3,
            "ranging: period: Input should be greater than or equal to 2",
        ),
        (1, {"ranging": {"strategy": "moon"}}, 3, "ranging: unknown strategy 'moon'; known: "),
        (1, {}, 0, "min_hold: Input should be greater than or equal to 1"),
    ],
)
async def test_auto_switching_refuses_what_it_cant_do(
    setup: Setup, bot_id: int, mapping: dict[str, Any], min_hold: int, error: str
) -> None:
    await setup.created()

    reply = await setup.control.handle(setup.auto(bot_id, min_hold=min_hold, **mapping))

    assert not reply.ok and reply.error is not None
    assert reply.error.startswith(error), reply.error
    assert (await setup.bot_row(1)).auto_switch is None


async def test_auto_switching_refuses_a_change_that_changes_nothing(setup: Setup) -> None:
    bot_id = await setup.created()

    off = await setup.control.handle(setup.auto(bot_id, off=True))
    assert (await setup.control.handle(setup.auto(bot_id))).ok
    same = await setup.control.handle(setup.auto(bot_id))

    assert (off.ok, off.error) == (False, "nothing to change: auto-switching is already off")
    assert (same.ok, same.error) == (
        False,
        "nothing to change: auto-switching already has those settings",
    )


async def test_a_regime_switch_is_dropped_if_the_bot_changed_meanwhile(
    setup: Setup, regimes: Regimes
) -> None:
    regimes.start[0] = "ranging"
    bot_id = await setup.created()
    assert (await setup.control.handle(setup.auto(bot_id, min_hold=2))).ok
    assert (await setup.control.handle(setup.command("start", bot_id))).ok  # the database only
    bot, _ = await load_bot(setup.db, "mexc", bot_id)
    assert bot.auto is not None
    check = bot.auto.check([candle(i) for i in range(5)], bot.strategy, MINUTE)
    assert check.switch is not None

    # Its settings changed before the switch was made.
    assert (await setup.control.handle(setup.auto(bot_id, min_hold=5))).ok
    with capture_logs() as changed:
        await setup.control.auto_switch(bot, check)
    # It was stopped before the switch was made.
    bot, _ = await load_bot(setup.db, "mexc", bot_id)
    assert (await setup.control.handle(setup.command("stop", bot_id))).ok
    with capture_logs() as stopped:
        await setup.control.auto_switch(bot, check)

    assert [
        log["reason"] for log in [*changed, *stopped] if log["event"] == "bot.auto_switch_dropped"
    ] == [
        "its auto-switching settings changed, or it was turned off",
        "the bot was stopped",
    ]
    assert len(await configs(setup)) == 1
    assert await switches(setup) == []


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
        rolled = await send(setup.redis, setup.rollback(created.bot_id, 1, "keep"), reply_within=5)

    assert started is not None and (started.ok, started.status) == (True, "running")
    assert refused is not None and refused.error == "a bot named 'btc-ema' already exists"
    assert switched is not None and (switched.ok, switched.strategy_config_id) == (True, 2)
    assert rolled is not None and (rolled.ok, rolled.strategy_config_id) == (True, 3)


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
