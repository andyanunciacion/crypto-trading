"""RedisQuotes reads what the price stream writes (a throwaway Redis, see shared/testing.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from engine.marketdata.prices import RedisPriceSink
from engine.marketdata.quotes import RedisQuotes
from shared.prices import PriceUpdate

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


async def test_reads_the_latest_price_the_stream_published(redis: Redis) -> None:
    update = PriceUpdate(
        exchange="mexc",
        symbol="BTC/USDT",
        bid=Decimal("81819.48"),
        ask=Decimal("81819.49"),
        bid_size=Decimal("0.12200121"),
        ask_size=Decimal("2.961881"),
        ts=T0,
        received_at=T0,
        source="ws",
    )
    await RedisPriceSink(redis).publish(update)

    assert await RedisQuotes(redis).latest("mexc", "BTC/USDT") == update


async def test_no_price_yet_is_none(redis: Redis) -> None:
    assert await RedisQuotes(redis).latest("mexc", "ETH/USDT") is None
