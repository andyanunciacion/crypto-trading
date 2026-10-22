"""RedisPriceSink against a real Redis (a throwaway container, see shared/testing.py)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from engine.marketdata.prices import RedisPriceSink
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
