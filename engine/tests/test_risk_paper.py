"""PaperDesk: paper bots are funded, trade through the RiskManager, and fill from live prices.

Against a throwaway TimescaleDB and Redis (see shared.testing). The price feed listens
on the same pub/sub channel the price stream publishes to.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import JsonValue
from redis.asyncio import Redis
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.brokers.base import BrokerOrder
from engine.exchange.models import Balance, MarketRules, OrderRequest
from engine.marketdata.prices import RedisPriceSink
from engine.risk.breakers import Swing
from engine.risk.paper import PaperDesk
from shared.db.session import make_engine
from shared.prices import PriceUpdate, price_channel
from shared.testing import insert_bot, set_limits

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
BTC = "BTC/USDT"
BOT = 1
REASON: dict[str, JsonValue] = {"signal": {"action": "buy", "reason": "test"}}


def quote(ask: str = "100") -> PriceUpdate:
    return PriceUpdate.model_validate(
        {
            "exchange": "mexc",
            "symbol": BTC,
            "bid": "99.9",
            "ask": ask,
            "bid_size": "5",
            "ask_size": "5",
            "ts": T0,
            "received_at": T0,
            "source": "ws",
        }
    )


@dataclass
class NoQuoteYet:
    """No price when the order is placed, so it rests until the feed brings one."""

    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        return None


@dataclass
class Rules:
    exchange_id: str = "mexc"

    async def market_rules(self, symbol: str) -> MarketRules:
        return MarketRules(
            symbol=symbol,
            amount_step=Decimal("0.0001"),
            price_step=Decimal("0.01"),
            min_amount=Decimal("0.0001"),
            min_cost=Decimal("1"),
        )


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE orders, fills, positions, paper_balances, risk_events"))
    await insert_bot(engine, BOT)
    await set_limits(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)  # bytes, as the engine's own client
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture
async def desk(db: AsyncEngine) -> PaperDesk:
    desk = PaperDesk(db, NoQuoteYet(), Rules(), clock=lambda: T0)
    await desk.fund(BOT, "USDT", Decimal(1000))
    return desk


async def resting_buy(desk: PaperDesk) -> BrokerOrder:
    request = OrderRequest.model_validate(
        {
            "symbol": BTC,
            "side": "buy",
            "amount": "0.5",
            "price": "100",
            "client_order_id": "b1-202610231200-b",
        }
    )
    placed = (await desk.risk.submit(BOT, request, REASON)).order
    assert placed is not None and placed.status == "open"
    return placed


async def subscribed(redis: Redis) -> None:
    """Wait until the feed listens, so nothing is published before anyone hears it."""
    channel = price_channel("mexc", BTC)
    async with asyncio.timeout(5):
        while (await redis.pubsub_numsub(channel))[0][1] == 0:  # noqa: ASYNC110 (polling Redis)
            await asyncio.sleep(0.01)


async def filled(desk: PaperDesk, client_order_id: str) -> BrokerOrder:
    async with asyncio.timeout(5):
        while True:
            order = await desk.risk.get_order(client_order_id)
            if order is not None and order.status == "closed":
                return order
            await asyncio.sleep(0.01)


async def test_funding_shows_in_the_balances(desk: PaperDesk) -> None:
    cash = Decimal(1000)
    assert await desk.risk.balances(BOT) == {
        "USDT": Balance(currency="USDT", free=cash, used=Decimal(0), total=cash)
    }


async def test_a_published_price_fills_a_resting_order(desk: PaperDesk, redis: Redis) -> None:
    placed = await resting_buy(desk)
    feed = asyncio.create_task(desk.feed_prices(redis, "mexc", [BTC]))
    try:
        await subscribed(redis)
        await RedisPriceSink(redis).publish(quote(ask="100"))

        done = await filled(desk, placed.client_order_id)
    finally:
        feed.cancel()

    # A resting order fills at its own limit, 100.
    assert [(f.price, f.amount) for f in done.fills] == [(Decimal(100), Decimal("0.5"))]
    # The price is noted for the spike breaker too (D42): its mid, 99.95.
    assert desk.swings.swing("mexc", BTC, T0) == Swing(low=Decimal("99.95"), high=Decimal("99.95"))


async def test_a_price_counts_for_the_spike_breaker_even_if_it_fills_nothing(
    desk: PaperDesk,
) -> None:
    await desk.on_price(quote(ask="100"))
    await desk.on_price(quote(ask="104"))  # nothing open: nothing to fill

    assert desk.swings.swing("mexc", BTC, T0) == Swing(low=Decimal("99.95"), high=Decimal("101.95"))


async def test_a_bad_update_is_logged_and_the_feed_carries_on(
    desk: PaperDesk, redis: Redis
) -> None:
    placed = await resting_buy(desk)
    feed = asyncio.create_task(desk.feed_prices(redis, "mexc", [BTC]))
    try:
        with capture_logs() as logs:
            await subscribed(redis)
            await redis.publish(price_channel("mexc", BTC), b"not a price")
            await RedisPriceSink(redis).publish(quote(ask="100"))
            await filled(desk, placed.client_order_id)
    finally:
        feed.cancel()

    errors = [log for log in logs if log["event"] == "paper.feed.error"]
    assert [(e["channel"], e["error"]) for e in errors] == [
        ("prices:mexc:BTC/USDT", "ValidationError")
    ]


async def test_each_price_goes_on_after_its_fills(desk: PaperDesk, redis: Redis) -> None:
    placed = await resting_buy(desk)
    seen: list[tuple[Decimal | None, str]] = []

    async def then(update: PriceUpdate) -> None:
        # What the bot runner's stops see: the order this price filled, already filled.
        order = await desk.risk.get_order(placed.client_order_id)
        assert order is not None
        seen.append((update.ask, order.status))
        if len(seen) == 1:
            raise RuntimeError("boom")

    feed = asyncio.create_task(desk.feed_prices(redis, "mexc", [BTC], then=then))
    try:
        with capture_logs() as logs:
            await subscribed(redis)
            await RedisPriceSink(redis).publish(quote(ask="100"))
            await RedisPriceSink(redis).publish(quote(ask="101"))
            async with asyncio.timeout(5):
                while len(seen) < 2:  # noqa: ASYNC110 (waiting on the feed task)
                    await asyncio.sleep(0.01)
    finally:
        feed.cancel()

    assert seen == [(Decimal(100), "closed"), (Decimal(101), "closed")]
    # The first one's error is logged, and the next price still goes on.
    errors = [log for log in logs if log["event"] == "paper.feed.listener_error"]
    assert [(e["channel"], e["error"], e["detail"]) for e in errors] == [
        ("prices:mexc:BTC/USDT", "RuntimeError", "boom")
    ]
