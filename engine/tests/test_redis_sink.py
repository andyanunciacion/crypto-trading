"""RedisPriceSink and RedisFormingSink against a real Redis (a throwaway container, see
shared/testing.py)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from engine.exchange.models import Candle
from engine.marketdata.forming import RedisFormingSink
from engine.marketdata.prices import RedisPriceSink
from shared.candles import FormingCandle, forming_key
from shared.prices import PriceUpdate, price_channel, price_key

BTC = "BTC/USDT"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


def update(bid: str) -> PriceUpdate:
    return PriceUpdate(
        exchange="mexc",
        symbol=BTC,
        bid=Decimal(bid),
        ask=Decimal("81819.49"),
        ts=T0,
        received_at=T0,
        source="ws",
    )


async def next_message(pubsub: PubSub) -> PriceUpdate:
    async with asyncio.timeout(5):
        while True:
            # Returns None for the subscribe confirmation, or when nothing came in time.
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
            if message is not None:
                return PriceUpdate.model_validate_json(message["data"])


async def test_latest_price_is_stored_and_published(redis: Redis) -> None:
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(price_channel("mexc", BTC))
        sink = RedisPriceSink(redis)

        await sink.publish(update("81819.48"))
        await sink.publish(update("81819.47"))

        stored = await redis.get(price_key("mexc", BTC))
        assert stored is not None
        assert PriceUpdate.model_validate_json(stored) == update("81819.47")  # the latest wins
        assert await redis.ttl(price_key("mexc", BTC)) == -1  # never expires
        # Every update reaches subscribers, in order.
        assert await next_message(pubsub) == update("81819.48")
        assert await next_message(pubsub) == update("81819.47")


async def test_the_forming_candle_is_stored_for_the_chart(redis: Redis) -> None:
    received = datetime(2026, 10, 22, 12, 30, 5, tzinfo=UTC)
    sink = RedisFormingSink(redis, "mexc", clock=lambda: received)
    kline = Candle(
        ts=T0,
        open=Decimal("81800"),
        high=Decimal("81900.5"),
        low=Decimal("81750"),
        close=Decimal("81819.48"),
        volume=Decimal("12.345"),
    )

    await sink(BTC, "1h", kline)

    stored = await redis.get(forming_key("mexc", BTC, "1h"))
    assert stored is not None
    assert FormingCandle.model_validate_json(stored) == FormingCandle(
        exchange="mexc",
        symbol=BTC,
        timeframe="1h",
        ts=T0,
        open=Decimal("81800"),
        high=Decimal("81900.5"),
        low=Decimal("81750"),
        close=Decimal("81819.48"),
        volume=Decimal("12.345"),
        received_at=received,
    )
    assert await redis.ttl(forming_key("mexc", BTC, "1h")) == -1  # readers judge its age
