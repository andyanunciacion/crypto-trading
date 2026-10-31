"""PaperBroker against a real TimescaleDB (see shared.testing), with prices from a fake.

Fee 0.1%. Each expected number is worked out by hand in the comment beside it.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, get_args

import pytest
from pydantic import JsonValue
from sqlalchemy import URL, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.brokers.base import (
    BrokerFill,
    BrokerOrderStatus,
    DuplicateClientOrderId,
    Mode,
    UnknownOrder,
    client_order_id,
)
from engine.brokers.paper import PaperBroker
from engine.exchange.models import CLIENT_ORDER_ID_PATTERN, Balance, MarketRules, OrderRequest
from engine.strategies.base import Position
from shared.db.models import ORDER_STATUSES, TRADING_MODES, OrderRow, PositionRow
from shared.db.session import make_engine
from shared.prices import PriceUpdate
from shared.testing import alert_of, insert_bot

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
BTC = "BTC/USDT"
BOT = 1
RULES = MarketRules(
    symbol=BTC,
    amount_step=Decimal("0.0001"),
    price_step=Decimal("0.01"),
    min_amount=Decimal("0.0001"),
    min_cost=Decimal("1"),
)
REASON: dict[str, JsonValue] = {"signal": {"action": "buy", "reason": "test"}}


def d(value: str) -> Decimal:
    return Decimal(value)


def quote(
    *,
    ask: str = "100",
    ask_size: str | None = "5",
    bid: str = "99.9",
    bid_size: str | None = "5",
    ts: datetime = T0,
) -> PriceUpdate:
    return PriceUpdate(
        exchange="mexc",
        symbol=BTC,
        bid=d(bid),
        ask=d(ask),
        bid_size=None if bid_size is None else d(bid_size),
        ask_size=None if ask_size is None else d(ask_size),
        ts=ts,
        received_at=ts,
        source="ws",
    )


def order(side: str, amount: str, price: str, cid: str = "b1-202610231200-b") -> OrderRequest:
    return OrderRequest.model_validate(
        {"symbol": BTC, "side": side, "amount": amount, "price": price, "client_order_id": cid}
    )


@dataclass
class FakeQuotes:
    price: PriceUpdate | None = field(default_factory=quote)

    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        assert (exchange, symbol) == ("mexc", BTC)
        return self.price


@dataclass
class FakeRules:
    exchange_id: str = "mexc"

    async def market_rules(self, symbol: str) -> MarketRules:
        assert symbol == BTC
        return RULES


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Setup:
    db: AsyncEngine
    quotes: FakeQuotes
    clock: Clock
    broker: PaperBroker

    def fresh_broker(self) -> PaperBroker:
        """Another PaperBroker on the same database, as after an engine restart."""
        return PaperBroker(self.db, self.quotes, FakeRules(), clock=self.clock)

    async def order_rows(self) -> int:
        async with self.db.connect() as conn:
            return (await conn.execute(select(func.count()).select_from(OrderRow))).scalar_one()


@pytest.fixture
async def setup(db_url: URL) -> AsyncIterator[Setup]:
    db = make_engine(db_url)
    async with db.begin() as conn:
        await conn.execute(text("TRUNCATE orders, fills, positions, paper_balances"))
    await insert_bot(db, BOT)
    quotes, clock = FakeQuotes(), Clock()
    broker = PaperBroker(db, quotes, FakeRules(), clock=clock)
    await broker.deposit(BOT, "USDT", d("1000"))
    yield Setup(db, quotes, clock, broker)
    await db.dispose()


def usdt(free: str, used: str = "0") -> Balance:
    return Balance(currency="USDT", free=d(free), used=d(used), total=d(free) + d(used))


def btc(free: str, used: str = "0") -> Balance:
    return Balance(currency="BTC", free=d(free), used=d(used), total=d(free) + d(used))


# --- Fills, fees and balances ----------------------------------------------------------


async def test_a_buy_fills_in_parts_at_the_ask_then_at_the_later_ask(setup: Setup) -> None:
    # Only 0.5 BTC on offer at the 100 ask, 2 wanted with a 101 limit.
    setup.quotes.price = quote(ask="100", ask_size="0.5")

    with capture_logs() as logs:
        placed = await setup.broker.place_order(BOT, order("buy", "2", "101"), REASON)

    # Reserved 101 x 2 x 1.001 = 202.202. Filled 0.5 at 100: released 50.5505,
    # paid 50 + 0.05 fee. Free: 1000 - 202.202 + 50.5505 - 50.05 = 798.2985.
    assert placed.status == "open"
    assert placed.filled == d("0.5")
    assert placed.fills == [
        BrokerFill(ts=T0, price=d("100"), amount=d("0.5"), fee=d("0.05"), fee_currency="USDT")
    ]
    assert await setup.broker.balances(BOT) == {
        "USDT": usdt("798.2985", "151.6515"),
        "BTC": btc("0.5"),
    }

    # Later the ask is 100.5 with plenty on offer. The order crossed when placed and
    # still does, so the rest keeps taking: at the 100.5 ask, not its 101 limit.
    setup.clock.now = T0 + timedelta(seconds=5)
    with capture_logs() as later:
        fills = await setup.broker.on_price(quote(ask="100.5", ask_size="3", ts=setup.clock.now))

    assert fills == [
        BrokerFill(
            ts=setup.clock.now,
            price=d("100.5"),
            amount=d("1.5"),
            fee=d("0.15075"),
            fee_currency="USDT",
        )
    ]
    done = await setup.broker.get_order(placed.client_order_id)
    assert done is not None
    assert (done.status, done.filled, len(done.fills)) == ("closed", d("2"), 2)
    # The 151.6515 still held (at the limit) is released; 1.5 x 100.5 = 150.75 + 0.15075
    # fee is paid. The 0.75075 left over comes back: 798.2985 + 0.75075 = 799.04925.
    assert await setup.broker.balances(BOT) == {"USDT": usdt("799.04925"), "BTC": btc("2")}
    # Entry (0.5 x 100 + 1.5 x 100.5) / 2 = 100.375: one buy, so that's what it paid too.
    assert await setup.broker.position(BOT, BTC) == Position(
        amount=d("2"), avg_entry=d("100.375"), last_entry=d("100.375")
    )
    # One trade alert, when its last slice filled (D59).
    filled = [e for e in [*logs, *later] if e["event"] == "broker.order.filled"]
    assert [alert_of(e) for e in filled] == [None, "trade"]


async def test_a_position_knows_what_its_last_buy_paid(setup: Setup) -> None:
    # A first position, bought at 100 and sold: its buy no longer counts.
    await setup.broker.place_order(BOT, order("buy", "1", "101"), REASON)
    await setup.broker.place_order(BOT, order("sell", "1", "99", "b1-202610231200-s"), REASON)
    assert await setup.broker.position(BOT, BTC) is None

    # Then 1 at 90, and a buy of 1 filled 0.5 at 80 and 0.5 at 82 (taking as the ask rose).
    setup.clock.now = T0 + timedelta(minutes=1)
    setup.quotes.price = quote(ask="90", ts=setup.clock.now)
    await setup.broker.place_order(BOT, order("buy", "1", "91", "b1-202610231201-b"), REASON)
    setup.clock.now = T0 + timedelta(minutes=2)
    setup.quotes.price = quote(ask="80", ask_size="0.5", ts=setup.clock.now)
    await setup.broker.place_order(BOT, order("buy", "1", "83", "b1-202610231202-b"), REASON)
    setup.clock.now += timedelta(seconds=1)
    await setup.broker.on_price(quote(ask="82", ts=setup.clock.now))

    # The last buy paid (0.5 x 80 + 0.5 x 82) / 1 = 81; the average is (90 + 81) / 2.
    assert await setup.broker.position(BOT, BTC) == Position(
        amount=d("2"), avg_entry=d("85.5"), last_entry=d("81")
    )


async def test_a_position_no_buy_filled_has_no_last_buy(setup: Setup) -> None:
    async with setup.db.begin() as conn:
        await conn.execute(
            insert(PositionRow).values(
                bot_id=BOT,
                mode="paper",
                exchange="mexc",
                symbol=BTC,
                side="long",
                amount=d("1"),
                avg_entry=d("100"),
                fees_paid=d("0"),
                opened_at=T0,
                realized_pnl=d("0"),
            )
        )

    assert await setup.broker.position(BOT, BTC) == Position(amount=d("1"), avg_entry=d("100"))


async def test_an_unchanged_quote_does_not_fill_the_same_size_again(setup: Setup) -> None:
    # MEXC re-sends an unchanged book ticker about every 100 ms. 0.5 at the ask, 2 wanted.
    setup.quotes.price = quote(ask="100", ask_size="0.5")
    placed = await setup.broker.place_order(BOT, order("buy", "2", "101"), REASON)
    assert placed.filled == d("0.5")

    # The same quote again (as the price channel delivers it): its 0.5 is already ours.
    assert await setup.broker.on_price(quote(ask="100", ask_size="0.5")) == []
    # 0.8 at the same price: only the 0.3 that's new.
    grown = await setup.broker.on_price(quote(ask="100", ask_size="0.8"))
    assert [(f.price, f.amount) for f in grown] == [(d("100"), d("0.3"))]
    # A new best price: its whole 0.5.
    moved = await setup.broker.on_price(quote(ask="100.2", ask_size="0.5"))
    assert [(f.price, f.amount) for f in moved] == [(d("100.2"), d("0.5"))]

    done = await setup.broker.get_order(placed.client_order_id)
    assert done is not None
    assert (done.status, done.filled) == ("open", d("1.3"))


async def test_a_taker_that_stops_crossing_rests_at_its_limit(setup: Setup) -> None:
    setup.quotes.price = quote(ask="100", ask_size="0.5")
    placed = await setup.broker.place_order(BOT, order("buy", "2", "101"), REASON)

    # The ask jumps past the 101 limit: live, the rest would now sit on the book at 101.
    assert await setup.broker.on_price(quote(ask="101.5", ask_size="3")) == []
    # So when the ask comes back, it fills at its limit like any resting order.
    fills = await setup.broker.on_price(quote(ask="100.5", ask_size="3"))

    assert [(f.price, f.amount) for f in fills] == [(d("101"), d("1.5"))]
    done = await setup.broker.get_order(placed.client_order_id)
    assert done is not None
    assert done.status == "closed"


async def test_a_round_trip_closes_the_position_with_pnl_net_of_fees(setup: Setup) -> None:
    await setup.broker.place_order(BOT, order("buy", "1", "100", cid="b1-1-b"), REASON)
    setup.quotes.price = quote(bid="111", bid_size="5")

    sold = await setup.broker.place_order(BOT, order("sell", "1", "110", cid="b1-2-s"), REASON)

    # Bought 1 at 100 (fee 0.1), sold at the 111 bid (fee 0.111).
    assert (sold.status, sold.fills[0].price, sold.fills[0].fee) == ("closed", d("111"), d("0.111"))
    assert await setup.broker.balances(BOT) == {"USDT": usdt("1010.789")}  # 1000 - 100.1 + 110.889
    assert await setup.broker.position(BOT, BTC) is None
    async with setup.db.connect() as conn:
        closed = (await conn.execute(text("SELECT * FROM positions"))).mappings().one()
    assert closed["realized_pnl"] == d("10.789")  # 11 - 0.1 - 0.111
    assert closed["fees_paid"] == d("0.211")
    assert closed["amount"] == 0
    assert closed["closed_at"] == T0


async def test_an_order_without_a_fresh_price_waits_for_one(setup: Setup) -> None:
    setup.quotes.price = quote(ts=T0 - timedelta(seconds=11))  # older than the 10 s limit

    placed = await setup.broker.place_order(BOT, order("buy", "1", "100"), REASON)

    assert (placed.status, placed.fills) == ("open", [])
    assert await setup.broker.balances(BOT) == {"USDT": usdt("899.9", "100.1")}
    # A stale update fills nothing either; the next fresh one does.
    assert await setup.broker.on_price(quote(ts=T0 - timedelta(seconds=11))) == []
    assert len(await setup.broker.on_price(quote())) == 1


async def test_an_order_with_no_price_at_all_rests(setup: Setup) -> None:
    setup.quotes.price = None

    placed = await setup.broker.place_order(BOT, order("buy", "1", "100"), REASON)

    assert (placed.status, placed.fills) == ("open", [])


async def test_no_size_on_offer_means_no_fill(setup: Setup) -> None:
    setup.quotes.price = quote(ask_size=None)

    placed = await setup.broker.place_order(BOT, order("buy", "1", "100"), REASON)

    assert placed.fills == []


# --- Retries never duplicate -----------------------------------------------------------


async def test_placing_the_same_order_again_returns_the_first(setup: Setup) -> None:
    request = order("buy", "1", "100")

    first = await setup.broker.place_order(BOT, request, REASON)
    again = await setup.broker.place_order(BOT, request, REASON)

    assert again == first
    assert await setup.order_rows() == 1
    assert await setup.broker.balances(BOT) == {"USDT": usdt("899.9"), "BTC": btc("1")}


async def test_the_same_order_placed_twice_at_once_is_placed_once(setup: Setup) -> None:
    # As if a timed-out call was retried while the first was still running.
    setup.quotes.price = None  # resting, so the reservation shows whether it happened twice
    request = order("buy", "1", "100")

    first, second = await asyncio.gather(
        setup.broker.place_order(BOT, request, REASON),
        setup.broker.place_order(BOT, request, REASON),
    )

    assert first == second
    assert await setup.order_rows() == 1
    assert await setup.broker.balances(BOT) == {"USDT": usdt("899.9", "100.1")}


async def test_a_reused_id_on_a_different_order_is_refused(setup: Setup) -> None:
    await setup.broker.place_order(BOT, order("buy", "1", "100"), REASON)

    with pytest.raises(DuplicateClientOrderId):
        await setup.broker.place_order(BOT, order("buy", "2", "100"), REASON)
    assert await setup.order_rows() == 1


# --- Rejections ------------------------------------------------------------------------


async def test_an_order_off_the_market_rules_is_rejected_and_kept(setup: Setup) -> None:
    request = order("buy", "0.00015", "100")  # off the 0.0001 amount step

    rejected = await setup.broker.place_order(BOT, request, REASON)

    assert rejected.status == "rejected"
    assert rejected.reject_reason is not None
    assert "amount step" in rejected.reject_reason
    assert await setup.broker.balances(BOT) == {"USDT": usdt("1000")}
    # Retrying gets the same answer, not a second try.
    assert await setup.broker.place_order(BOT, request, REASON) == rejected


async def test_an_order_without_enough_balance_is_rejected(setup: Setup) -> None:
    rejected = await setup.broker.place_order(BOT, order("buy", "10", "100"), REASON)

    assert rejected.status == "rejected"
    assert rejected.reject_reason == "needs 1001.000 USDT, 1000 free"
    assert await setup.broker.balances(BOT) == {"USDT": usdt("1000")}


async def test_selling_coins_the_bot_doesnt_have_is_rejected(setup: Setup) -> None:
    rejected = await setup.broker.place_order(BOT, order("sell", "1", "100"), REASON)

    assert rejected.status == "rejected"
    assert rejected.reject_reason == "needs 1 BTC, 0 free"


# --- Cancelling ------------------------------------------------------------------------


async def test_cancelling_releases_what_the_rest_of_the_order_held(setup: Setup) -> None:
    setup.quotes.price = quote(ask_size="0.25")
    placed = await setup.broker.place_order(BOT, order("buy", "1", "100"), REASON)

    with capture_logs() as logs:
        canceled = await setup.broker.cancel_order(placed.client_order_id)

    # 0.25 filled at 100 (25 + 0.025 fee); the 0.75 x 100 x 1.001 still held comes back.
    assert (canceled.status, canceled.filled) == ("canceled", d("0.25"))
    # What filled traded: a trade alert (D59).
    assert [alert_of(e) for e in logs if e["event"] == "broker.order.canceled"] == ["trade"]
    assert await setup.broker.balances(BOT) == {"USDT": usdt("974.975"), "BTC": btc("0.25")}
    # Cancelling again changes nothing.
    assert await setup.broker.cancel_order(placed.client_order_id) == canceled
    assert await setup.broker.balances(BOT) == {"USDT": usdt("974.975"), "BTC": btc("0.25")}
    # And a canceled order no longer fills.
    assert await setup.broker.on_price(quote()) == []


async def test_cancelling_an_unknown_order_is_an_error(setup: Setup) -> None:
    with pytest.raises(UnknownOrder):
        await setup.broker.cancel_order("b1-nothing")


# --- Restart ---------------------------------------------------------------------------


async def test_a_new_broker_carries_on_from_the_database(setup: Setup) -> None:
    setup.quotes.price = quote(ask_size="0.4")
    placed = await setup.broker.place_order(BOT, order("buy", "1", "101"), REASON)

    restarted = setup.fresh_broker()

    assert await restarted.get_order(placed.client_order_id) == placed
    assert await restarted.balances(BOT) == await setup.broker.balances(BOT)
    assert await restarted.position(BOT, BTC) == Position(
        amount=d("0.4"), avg_entry=d("100"), last_entry=d("100")
    )
    # The open rest still fills. Which orders were taking isn't stored, so after a
    # restart it rests and fills at its 101 limit, not the 100.5 ask: pessimistic.
    fills = await restarted.on_price(quote(ask="100.5"))
    assert [(f.price, f.amount) for f in fills] == [(d("101"), d("0.6"))]


async def test_a_crash_in_the_middle_of_a_fill_writes_none_of_it(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup.quotes.price = quote(ask_size="0.4")
    placed = await setup.broker.place_order(BOT, order("buy", "1", "101"), REASON)
    balances = await setup.broker.balances(BOT)

    # The engine dies after the fill row and balances are written, before the position.
    async def killed(*args: object) -> None:
        raise RuntimeError("killed")

    monkeypatch.setattr(setup.broker, "_update_position", killed)
    with pytest.raises(RuntimeError, match="killed"):
        await setup.broker.on_price(quote(ask="100.5"))

    # One transaction: the fill, the order's progress and the balances are all undone.
    restarted = setup.fresh_broker()
    assert await restarted.get_order(placed.client_order_id) == placed
    assert await restarted.balances(BOT) == balances
    assert await restarted.position(BOT, BTC) == Position(
        amount=d("0.4"), avg_entry=d("100"), last_entry=d("100")
    )
    # So the restarted engine fills the rest once, not on top of a half-written fill.
    fills = await restarted.on_price(quote(ask="100.5"))
    assert [(f.price, f.amount) for f in fills] == [(d("101"), d("0.6"))]


# --- Concurrency -----------------------------------------------------------------------


async def test_a_quote_that_moves_while_a_new_order_fills_doesnt_break_it(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The 24-hour run's race: while a new order's first fill waits on the database, the
    # price feed handles a quote whose ask moved. It can't see the uncommitted order.
    setup.quotes.price = quote(ask="100", ask_size="0.4")
    real = setup.broker._update_position
    meanwhile: list[list[BrokerFill]] = []

    async def interleaved(*args: Any) -> None:
        meanwhile.append(await setup.broker.on_price(quote(ask="100.5", ask_size="3")))
        await real(*args)

    monkeypatch.setattr(setup.broker, "_update_position", interleaved)
    with capture_logs() as logs:
        placed = await setup.broker.place_order(BOT, order("buy", "1", "101"), REASON)
    monkeypatch.undo()

    assert meanwhile == [[]]
    assert (placed.status, placed.filled) == ("open", d("0.4"))
    filled = [e for e in logs if e["event"] == "broker.order.filled"]
    assert [d(e["taken_at_best"]) for e in filled] == [d("0.4")]
    # Still taking, and nothing counted at 100.5 yet: the rest fills there.
    fills = await setup.broker.on_price(quote(ask="100.5", ask_size="3"))
    assert [(f.price, f.amount) for f in fills] == [(d("100.5"), d("0.6"))]
    # Paid 0.4 x 100 + 0.04 fee + 0.6 x 100.5 + 0.0603 fee = 100.4003; 1000 - 100.4003.
    assert await setup.broker.balances(BOT) == {"USDT": usdt("899.5997"), "BTC": btc("1")}


async def test_a_rolled_back_order_leaves_nothing_behind(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup.quotes.price = quote(ask="100", ask_size="0.4")

    async def killed(*args: object) -> None:
        raise RuntimeError("killed")

    monkeypatch.setattr(setup.broker, "_update_position", killed)
    with capture_logs() as logs, pytest.raises(RuntimeError, match="killed"):
        await setup.broker.place_order(BOT, order("buy", "1", "101"), REASON)
    monkeypatch.undo()

    assert await setup.order_rows() == 0
    assert await setup.broker.balances(BOT) == {"USDT": usdt("1000")}
    rolled_back = [e for e in logs if e["event"] == "broker.order.rolled_back"]
    assert [e["client_order_id"] for e in rolled_back] == ["b1-202610231200-b"]
    # The same ID again, now below the 102 ask: it rests. The failed try crossed, but
    # that mustn't make this one a taker, so it fills at its 101 limit, not the ask.
    setup.quotes.price = quote(ask="102")
    placed = await setup.broker.place_order(BOT, order("buy", "1", "101"), REASON)
    assert (placed.status, placed.filled) == ("open", d("0"))
    fills = await setup.broker.on_price(quote(ask="100.5"))
    assert [(f.price, f.amount) for f in fills] == [(d("101"), d("1"))]


# --- Client order IDs ------------------------------------------------------------------


def test_client_order_ids_come_from_the_bot_the_candle_and_the_side() -> None:
    cid = client_order_id(12, datetime(2026, 10, 23, 14, 0, tzinfo=UTC), "buy")

    assert cid == "b12-202610231400-b"
    assert client_order_id(12, datetime(2026, 10, 23, 14, 0, tzinfo=UTC), "buy") == cid
    others = {
        client_order_id(13, datetime(2026, 10, 23, 14, 0, tzinfo=UTC), "buy"),
        client_order_id(12, datetime(2026, 10, 23, 15, 0, tzinfo=UTC), "buy"),
        client_order_id(12, datetime(2026, 10, 23, 14, 0, tzinfo=UTC), "sell"),
    }
    assert cid not in others
    assert len(others) == 3


def test_client_order_ids_fit_the_exchange_pattern() -> None:
    longest = client_order_id(10**15, datetime(2026, 10, 23, tzinfo=UTC), "sell")
    assert re.fullmatch(CLIENT_ORDER_ID_PATTERN, longest)
    with pytest.raises(ValueError, match="positive"):
        client_order_id(0, T0, "buy")


def test_modes_and_statuses_match_the_database_checks() -> None:
    assert get_args(Mode) == TRADING_MODES
    assert get_args(BrokerOrderStatus) == ORDER_STATUSES
