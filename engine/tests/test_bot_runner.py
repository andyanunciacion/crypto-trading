"""BotRunner: closed candles in, orders through the RiskManager out, against a real
TimescaleDB (see shared.testing) and a real PaperDesk. The market (REST candles, market
rules) and the latest price are fakes; candles are fed straight to `on_closed`, or by
an injected stream instead of the WebSocket one.

EMA crossover with fast 3, slow 5 (warm-up 16): 30 flat candles at 100, then a close at
110 crosses the fast EMA above the slow one (BUY), and a close at 90 right after
crosses it back below (SELL). Fee 0.1%, slippage 0.05% (BTC/USDT).
"""

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import URL, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.bots.runner import BotRunner
from engine.bots.store import Bot, create_bot, load_running_bots
from engine.exchange.models import Balance, Candle, MarketRules, Timeframe
from engine.marketdata.store import CandleStore, Series
from engine.risk.paper import PaperDesk
from engine.risk.sizing import ALL_IN, AtrRisk, PercentOfEquity, Sizing
from engine.strategies.base import Action, Position, Signal, Strategy, StrategyParams
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from shared.db.models import BotRow, OrderRow
from shared.db.session import make_engine
from shared.prices import PriceUpdate

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


def d(value: str) -> Decimal:
    return Decimal(value)


def candle(i: int, close: str = "100") -> Candle:
    price = d(close)
    return Candle(ts=T0 + i * MINUTE, open=price, high=price, low=price, close=price, volume=d("1"))


HISTORY = [candle(i) for i in range(30)]  # flat: candles 0-29


@dataclass
class Clock:
    now: datetime = T0 + 30 * MINUTE + timedelta(seconds=2)  # candle 29 has just closed

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeMarket:
    """REST candles from a list, and fixed market rules. No WebSocket: tests feed candles."""

    rest: list[Candle] = field(default_factory=lambda: list(HISTORY))
    exchange_id: str = "mexc"

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        assert (symbol, timeframe) == (BTC, "1m")
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

    def set(self, *, ask: str, bid: str, size: str | None = "50") -> None:
        """A fresh book ticker: `size` on offer at each side (None: unknown, so no fill)."""
        self.price = PriceUpdate(
            exchange="mexc",
            symbol=BTC,
            ask=d(ask),
            bid=d(bid),
            ask_size=None if size is None else d(size),
            bid_size=None if size is None else d(size),
            ts=self.clock.now,
            received_at=self.clock.now,
            source="ws",
        )

    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        assert (exchange, symbol) == ("mexc", BTC)
        return self.price


@dataclass
class Setup:
    db: AsyncEngine
    clock: Clock
    quotes: FakeQuotes
    market: FakeMarket
    desk: PaperDesk

    def runner(self, bots: Sequence[Bot], **options: Any) -> BotRunner:
        return BotRunner(
            bots,
            self.desk.risk,
            self.market,
            self.quotes,
            CandleStore(self.db),
            self.db,
            clock=self.clock,
            **options,
        )

    async def ema_bot(
        self, name: str = "btc-ema", cash: str = "1000", sizing: Sizing = ALL_IN
    ) -> Bot:
        strategy = EmaCrossover(EmaCrossoverParams(fast=3, slow=5))
        bot_id = await create_bot(
            self.db,
            name=name,
            exchange="mexc",
            symbol=BTC,
            timeframe="1m",
            strategy=strategy,
            created_by="test",
            sizing=sizing,
        )
        await self.desk.fund(bot_id, "USDT", d(cash))
        (bot,) = [b for b in await load_running_bots(self.db, "mexc") if b.id == bot_id]
        return bot

    def tick(self) -> None:
        """One minute on: the next candle has just closed."""
        self.clock.now += MINUTE

    async def restart(self) -> BotRunner:
        """A new desk and runner on the same database, as after the engine was killed.

        Nothing of the old ones is cleaned up or carried over: only the database is.
        """
        self.desk = PaperDesk(self.db, self.quotes, self.market, clock=self.clock)
        return self.runner(await load_running_bots(self.db, "mexc"))

    async def order_rows(self) -> int:
        async with self.db.connect() as conn:
            return (await conn.execute(select(func.count()).select_from(OrderRow))).scalar_one()


@pytest.fixture
async def setup(db_url: URL) -> AsyncIterator[Setup]:
    db = make_engine(db_url)
    async with db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " candles RESTART IDENTITY"
            )
        )
    clock = Clock()
    quotes = FakeQuotes(clock)
    market = FakeMarket()
    yield Setup(db, clock, quotes, market, PaperDesk(db, quotes, market, clock=clock))
    await db.dispose()


def usdt(free: str, used: str = "0") -> Balance:
    return Balance(currency="USDT", free=d(free), used=d(used), total=d(free) + d(used))


# --- Trading -----------------------------------------------------------------------------


async def test_a_bot_buys_on_a_cross_up_and_sells_on_the_cross_down(setup: Setup) -> None:
    bot = await setup.ema_bot()
    runner = setup.runner([bot])
    assert await runner.warm_up(SERIES) == candle(29).ts
    risk = setup.desk.risk

    setup.tick()
    setup.quotes.set(ask="110", bid="109.9")
    await runner.on_closed(SERIES, candle(30, "110"))

    # Limit 110 x 1.0005 = 110.055 -> 110.06. Amount 1000 / (110.06 x 1.001) = 9.0768...
    # Fills at the 110 ask: costs 998.448 + 0.998448 fee.
    buy = await risk.get_order("b1-202610221230-b")
    assert buy is not None
    assert (buy.side, buy.price, buy.amount, buy.status) == (
        "buy",
        d("110.06"),
        d("9.0768"),
        "closed",
    )
    assert [(f.price, f.amount, f.fee) for f in buy.fills] == [
        (d("110"), d("9.0768"), d("0.9984480"))
    ]
    assert await risk.position(bot.id, BTC) == Position(amount=d("9.0768"), avg_entry=d("110"))
    # The order records why: the signal, the risk check, the bot and its config.
    signal = buy.reason["signal"]
    assert isinstance(signal, dict) and signal["action"] == "buy"
    assert signal["ts"] == "2026-10-22T12:30:00Z"
    assert buy.reason["risk"] == {
        "decision": "allowed",
        "rule": "pass_through",
        "detail": "stub: every order is allowed until P4",
    }
    assert buy.reason["bot"] == {"id": bot.id, "name": "btc-ema"}
    assert buy.reason["strategy_config"] == {
        "id": bot.strategy_config_id,
        "strategy": "ema_crossover",
        "version": 1,
        "params": {"fast": 3, "slow": 5},
    }

    setup.tick()
    setup.quotes.set(ask="90.1", bid="90")
    await runner.on_closed(SERIES, candle(31, "90"))

    # Limit 90 x 0.9995 = 89.955 -> 89.95. Sells all 9.0768 at the 90 bid: 816.912, fee 0.816912.
    sell = await risk.get_order("b1-202610221231-s")
    assert sell is not None
    assert (sell.side, sell.price, sell.amount, sell.status) == (
        "sell",
        d("89.95"),
        d("9.0768"),
        "closed",
    )
    assert await risk.position(bot.id, BTC) is None
    # 1000 - 998.448 - 0.998448 + 816.912 - 0.816912 = 816.64864
    assert await risk.balances(bot.id) == {"USDT": usdt("816.64864")}


async def test_a_bot_buys_what_its_sizing_says(setup: Setup) -> None:
    bot = await setup.ema_bot(sizing=PercentOfEquity(percent=d("25")))
    runner = setup.runner([bot])
    await runner.warm_up(SERIES)

    setup.tick()
    setup.quotes.set(ask="110", bid="109.9")
    await runner.on_closed(SERIES, candle(30, "110"))

    # 25% of 1000 = 250 at the 110.06 limit: 250 / 110.06 = 2.27148... -> 2.2714.
    buy = await setup.desk.risk.get_order("b1-202610221230-b")
    assert buy is not None and (buy.amount, buy.status) == (d("2.2714"), "closed")
    assert buy.reason["sizing"] == {
        "settings": {"mode": "percent_of_equity", "percent": "25"},
        "detail": "percent_of_equity: 25% of 1000 equity = 250 at 110.06 = 2.2714",
    }


def ranged(i: int, close: str = "100") -> Candle:
    # A candle spanning 1 either side of its close: a true range of 2 in a flat market.
    price = d(close)
    return Candle(
        ts=T0 + i * MINUTE, open=price, high=price + 1, low=price - 1, close=price, volume=d("1")
    )


async def test_an_atr_bot_warms_up_enough_candles_for_its_atr(setup: Setup) -> None:
    # ATR over 3 candles needs 6 x 3 + 1 = 19, more than EMA's 16: warm-up fetches 11-29.
    setup.market.rest = [ranged(i) for i in range(30)]
    bot = await setup.ema_bot(sizing=AtrRisk(period=3))
    runner = setup.runner([bot])
    await runner.warm_up(SERIES)

    setup.tick()
    setup.quotes.set(ask="110", bid="109.9")
    await runner.on_closed(SERIES, ranged(30, "110"))

    # True ranges are 2 until candle 30 jumps from 100 to 110: max(2, 111 - 100, ...) = 11.
    # ATR: 2, then 2 + (11 - 2) / 3 = 5. 1% of 1000 = 10 at risk / (2 x 5) = 1 BTC.
    buy = await setup.desk.risk.get_order("b1-202610221230-b")
    assert buy is not None and buy.amount == d("1")


async def test_whats_left_of_an_order_is_cancelled_at_the_next_candle(setup: Setup) -> None:
    bot = await setup.ema_bot()
    runner = setup.runner([bot])
    await runner.warm_up(SERIES)

    setup.tick()
    setup.quotes.set(ask="110", bid="109.9", size="4")  # only 4 BTC on offer at the ask
    await runner.on_closed(SERIES, candle(30, "110"))
    partly = await setup.desk.risk.get_order("b1-202610221230-b")
    assert partly is not None and (partly.status, partly.filled) == ("open", d("4"))

    setup.tick()
    with capture_logs() as logs:
        await runner.on_closed(SERIES, candle(31, "110"))  # no cross: HOLD

    done = await setup.desk.risk.get_order("b1-202610221230-b")
    assert done is not None and (done.status, done.filled) == ("canceled", d("4"))
    assert [log["client_order_id"] for log in logs if log["event"] == "risk.cancel"] == [
        "b1-202610221230-b"
    ]
    # Nothing is held for it any more. 1000 - 440 - 0.44 fee = 559.56; 4 BTC bought.
    assert await setup.desk.risk.balances(bot.id) == {
        "USDT": usdt("559.56"),
        "BTC": Balance(currency="BTC", free=d("4"), used=d("0"), total=d("4")),
    }
    assert await setup.desk.risk.position(bot.id, BTC) == Position(
        amount=d("4"), avg_entry=d("110")
    )


async def test_no_price_means_no_order_and_says_why(setup: Setup) -> None:
    bot = await setup.ema_bot()
    runner = setup.runner([bot])
    await runner.warm_up(SERIES)

    setup.tick()
    with capture_logs() as logs:
        await runner.on_closed(SERIES, candle(30, "110"))  # BUY, but no price in Redis

    skipped = [log for log in logs if log["event"] == "bot.skipped"]
    assert [(s["bot_id"], s["side"], s["reason"]) for s in skipped] == [
        (bot.id, "buy", "no price for BTC/USDT")
    ]
    assert await setup.desk.risk.get_order("b1-202610221230-b") is None


# --- Isolation ---------------------------------------------------------------------------


class Exploding(Strategy[StrategyParams]):
    name = "exploding"
    version = 1
    params_model = StrategyParams

    @property
    def warmup_candles(self) -> int:
        return 1

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        raise RuntimeError("boom")


async def test_a_failing_bot_doesnt_stop_the_others(setup: Setup) -> None:
    broken = Bot(
        id=99,
        name="broken",
        exchange="mexc",
        symbol=BTC,
        timeframe="1m",
        strategy_config_id=99,
        strategy=Exploding(StrategyParams()),
    )
    bot = await setup.ema_bot()
    runner = setup.runner([broken, bot])  # the broken one goes first
    await runner.warm_up(SERIES)

    setup.tick()
    setup.quotes.set(ask="110", bid="109.9")
    with capture_logs() as logs:
        await runner.on_closed(SERIES, candle(30, "110"))

    errors = [log for log in logs if log["event"] == "bot.error"]
    assert [(e["bot_id"], e["error"], e["detail"]) for e in errors] == [
        (99, "RuntimeError", "boom")
    ]
    buy = await setup.desk.risk.get_order("b1-202610221230-b")
    assert buy is not None and buy.status == "closed"


# --- Candles -----------------------------------------------------------------------------


async def test_warm_up_seeds_from_rest_and_every_candle_is_stored(setup: Setup) -> None:
    bot = await setup.ema_bot()
    runner = setup.runner([bot])
    store = CandleStore(setup.db)

    await runner.warm_up(SERIES)
    # 16 warm-up candles: 14 to 29.
    assert await store.candles(SERIES, T0, T0 + 30 * MINUTE) == HISTORY[14:]

    setup.tick()
    await runner.on_closed(SERIES, candle(30, "101"))
    assert await store.candles(SERIES, T0 + 30 * MINUTE, T0 + 31 * MINUTE) == [candle(30, "101")]


async def test_warm_up_when_rest_lacks_the_newest_candle(setup: Setup) -> None:
    setup.market.rest = HISTORY[:-1]  # candle 29 closed 2 s ago, REST doesn't have it yet
    runner = setup.runner([await setup.ema_bot()])

    # The stream carries on after candle 28, so it fetches 29 first.
    assert await runner.warm_up(SERIES) == candle(28).ts


async def test_run_follows_the_stream(setup: Setup) -> None:
    bot = await setup.ema_bot()
    asked: list[tuple[str, Timeframe, datetime]] = []

    async def stream(symbol: str, timeframe: Timeframe, last: datetime) -> AsyncGenerator[Candle]:
        asked.append((symbol, timeframe, last))
        setup.tick()
        setup.quotes.set(ask="110", bid="109.9")
        yield candle(30, "110")

    await setup.runner([bot], stream=stream).run()  # returns when the stream ends

    assert asked == [(BTC, "1m", candle(29).ts)]
    buy = await setup.desk.risk.get_order("b1-202610221230-b")
    assert buy is not None and buy.status == "closed"


# --- Restart -----------------------------------------------------------------------------


async def test_a_killed_engine_carries_on_from_the_database(setup: Setup) -> None:
    bot = await setup.ema_bot()
    runner = setup.runner([bot])
    await runner.warm_up(SERIES)
    setup.tick()
    setup.quotes.set(ask="110", bid="109.9", size="4")  # 4 of the 9.0768 BTC wanted
    await runner.on_closed(SERIES, candle(30, "110"))
    risk = setup.desk.risk
    leftover = await risk.get_order("b1-202610221230-b")
    assert leftover is not None and (leftover.status, leftover.filled) == ("open", d("4"))
    position = await risk.position(bot.id, BTC)
    before = await risk.balances(bot.id)

    # Killed mid-candle; back 30 s later, when REST has candle 30 too.
    setup.market.rest.append(candle(30, "110"))
    setup.clock.now += timedelta(seconds=30)
    restarted = await setup.restart()
    with capture_logs() as logs:
        await restarted.recover()

    # The leftover is cancelled before any price can fill it, and its cash released.
    risk = setup.desk.risk
    canceled = await risk.get_order("b1-202610221230-b")
    assert canceled is not None and (canceled.status, canceled.filled) == ("canceled", d("4"))
    assert (
        await risk.position(bot.id, BTC) == position == Position(amount=d("4"), avg_entry=d("110"))
    )
    # 1000 - 440 - 0.44 fee = 559.56, nothing held any more; totals as before the kill.
    after = await risk.balances(bot.id)
    assert after == {
        "USDT": usdt("559.56"),
        "BTC": Balance(currency="BTC", free=d("4"), used=d("0"), total=d("4")),
    }
    assert {c: b.total for c, b in after.items()} == {c: b.total for c, b in before.items()}
    (recovered,) = [log for log in logs if log["event"] == "bot.recovered"]
    assert (recovered["bot_id"], recovered["canceled"]) == (bot.id, ["b1-202610221230-b"])
    # As psql shows them, so they compare with a snapshot by eye: no "0E-7" for nothing held.
    logged = recovered["balances"]
    assert all("E" not in v for b in logged.values() for v in b.values()), logged
    assert {c: {k: d(v) for k, v in b.items()} for c, b in logged.items()} == {
        "USDT": {"free": d("559.56"), "used": d("0")},
        "BTC": {"free": d("4"), "used": d("0")},
    }

    # Candle 30 only seeds the window: it was traded before the kill, not again now.
    assert await restarted.warm_up(SERIES) == candle(30).ts
    assert await setup.order_rows() == 1

    # The next candle crosses down: it sells exactly the 4 BTC the database holds.
    setup.tick()
    setup.quotes.set(ask="90.1", bid="90")
    await restarted.on_closed(SERIES, candle(31, "90"))
    sell = await risk.get_order("b1-202610221231-s")
    assert sell is not None and (sell.amount, sell.status) == (d("4"), "closed")
    assert await risk.position(bot.id, BTC) is None
    # 559.56 + 4 x 90 = 360, less the 0.36 fee: 919.2.
    assert await risk.balances(bot.id) == {"USDT": usdt("919.2")}


async def test_a_stopped_bots_open_order_is_cancelled_at_startup(setup: Setup) -> None:
    stopped = await setup.ema_bot()
    runner = setup.runner([stopped])
    await runner.warm_up(SERIES)
    setup.tick()
    setup.quotes.set(ask="110", bid="109.9", size="4")
    await runner.on_closed(SERIES, candle(30, "110"))
    # Stopped as CLAUDE.md says (no stop command until P5), with another bot running.
    other = await setup.ema_bot("other")
    async with setup.db.begin() as conn:
        await conn.execute(update(BotRow).where(BotRow.id == stopped.id).values(status="stopped"))

    restarted = await setup.restart()
    await restarted.recover()

    order = await setup.desk.risk.get_order("b1-202610221230-b")
    assert order is not None and order.status == "canceled"
    assert (await setup.desk.risk.balances(stopped.id))["USDT"] == usdt("559.56")
    assert await setup.desk.risk.balances(other.id) == {"USDT": usdt("1000")}


# --- Catch-up ----------------------------------------------------------------------------


class BuysWhenFlat(Strategy[StrategyParams]):
    """Buys on every candle while flat, and remembers each window it decided on."""

    name = "buys_when_flat"
    version = 1
    params_model = StrategyParams

    def __init__(self, params: StrategyParams) -> None:
        super().__init__(params)
        self.windows: list[list[datetime]] = []

    @property
    def warmup_candles(self) -> int:
        return 3

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        self.windows.append([c.ts for c in window])
        action: Action = "buy" if position is None else "hold"
        return Signal(action=action, ts=window[-1].ts, reason="test")


async def test_after_an_outage_a_bot_acts_on_the_newest_candle_only(setup: Setup) -> None:
    strategy = BuysWhenFlat(StrategyParams())
    bot = replace(await setup.ema_bot(), strategy=strategy)

    async def stream(symbol: str, timeframe: Timeframe, last: datetime) -> AsyncGenerator[Candle]:
        # The network was out for 5 minutes: candles 30-34 arrive at once, just after
        # candle 34 closed. Only 34 closed less than a minute ago.
        setup.clock.now = T0 + 35 * MINUTE + timedelta(seconds=2)
        setup.quotes.set(ask="110", bid="109.9")
        for i in range(30, 35):
            yield candle(i, "110")

    with capture_logs() as logs:
        await setup.runner([bot], stream=stream).run()

    caught_up = [log for log in logs if log["event"] == "bot.caught_up"]
    assert [log["candle_ts"] for log in caught_up] == [
        candle(i).ts.isoformat() for i in range(30, 34)
    ]
    # The missed candles filled the window; the strategy decided once, on the newest.
    assert strategy.windows == [[candle(i).ts for i in (32, 33, 34)]]
    assert await setup.order_rows() == 1
    buy = await setup.desk.risk.get_order("b1-202610221234-b")
    assert buy is not None and buy.status == "closed"
    # Every candle is still stored.
    stored = await CandleStore(setup.db).candles(SERIES, T0 + 30 * MINUTE, T0 + 35 * MINUTE)
    assert [c.ts for c in stored] == [candle(i).ts for i in range(30, 35)]


async def test_a_leftover_order_is_cancelled_at_the_first_missed_candle(setup: Setup) -> None:
    bot = await setup.ema_bot()
    runner = setup.runner([bot])
    await runner.warm_up(SERIES)
    setup.tick()
    setup.quotes.set(ask="110", bid="109.9", size="4")
    await runner.on_closed(SERIES, candle(30, "110"))  # BUY, 4 BTC filled, the rest open

    setup.clock.now = T0 + 35 * MINUTE + timedelta(seconds=2)  # back after 4 minutes
    with capture_logs() as logs:
        for i in range(31, 35):
            await runner.on_closed(SERIES, candle(i, "110"))

    caught_up = [
        (log["candle_ts"], log["canceled"]) for log in logs if log["event"] == "bot.caught_up"
    ]
    assert caught_up == [
        (candle(31).ts.isoformat(), ["b1-202610221230-b"]),
        (candle(32).ts.isoformat(), []),
        (candle(33).ts.isoformat(), []),
    ]
    leftover = await setup.desk.risk.get_order("b1-202610221230-b")
    assert leftover is not None and (leftover.status, leftover.filled) == ("canceled", d("4"))
    assert await setup.order_rows() == 1  # candle 34 is acted on: no cross, so HOLD


async def test_a_candle_rest_had_late_is_not_traded(setup: Setup) -> None:
    bot = await setup.ema_bot()
    runner = setup.runner([bot])
    await runner.warm_up(SERIES)

    # REST lacked candle 30 at its close (candles.live.late), so it comes at the next
    # one with candle 31: a minute late, after the backtest would have filled it.
    setup.clock.now = T0 + 32 * MINUTE + timedelta(seconds=2)
    setup.quotes.set(ask="110", bid="109.9")
    with capture_logs() as logs:
        await runner.on_closed(SERIES, candle(30, "110"))  # the cross up: BUY if traded
        await runner.on_closed(SERIES, candle(31, "110"))  # no new cross: HOLD

    assert [log["candle_ts"] for log in logs if log["event"] == "bot.caught_up"] == [
        candle(30).ts.isoformat()
    ]
    assert await setup.order_rows() == 0
