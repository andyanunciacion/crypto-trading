"""CandleStore against a real TimescaleDB (see the root conftest.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import URL, text

from engine.exchange.models import Candle
from engine.marketdata.store import CandleStore, Gap, Series
from shared.db.session import make_engine

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
BTC = Series("mexc", "BTC/USDT", "1h")


def candle(hour: int, close: str = "100") -> Candle:
    price = Decimal(close)
    return Candle(
        ts=T0 + hour * HOUR, open=price, high=price, low=price, close=price, volume=Decimal(1)
    )


@pytest.fixture
async def store(db_url: URL) -> AsyncIterator[CandleStore]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE candles"))
    yield CandleStore(engine)
    await engine.dispose()


async def test_upsert_is_idempotent(store: CandleStore) -> None:
    candles = [candle(h) for h in range(5)]

    await store.upsert(BTC, candles)
    await store.upsert(BTC, candles)

    bounds = await store.bounds(BTC)
    assert bounds is not None and bounds.count == 5
    assert await store.candles(BTC, T0, T0 + 5 * HOUR) == candles


async def test_upsert_overwrites_a_stored_candle(store: CandleStore) -> None:
    await store.upsert(BTC, [candle(0, close="100")])
    await store.upsert(BTC, [candle(0, close="105")])

    assert await store.candles(BTC, T0, T0 + HOUR) == [candle(0, close="105")]


async def test_decimals_round_trip_exactly(store: CandleStore) -> None:
    tiny = Candle(
        ts=T0,
        open=Decimal("0.000000012345678901234567"),
        high=Decimal("0.00000002"),
        low=Decimal("0.00000001"),
        close=Decimal("0.000000015"),
        volume=Decimal("123456789012345678.123456789"),
    )

    await store.upsert(BTC, [tiny])

    assert await store.candles(BTC, T0, T0 + HOUR) == [tiny]


async def test_find_gaps(store: CandleStore) -> None:
    # Hours 10-19 and 23-24 and 30, so gaps at 20-22 and 25-29. Nothing before hour 10
    # is stored; that isn't a gap.
    await store.upsert(BTC, [candle(h) for h in [*range(10, 20), 23, 24, 30]])

    assert await store.find_gaps(BTC) == [
        Gap(start=T0 + 20 * HOUR, end=T0 + 23 * HOUR, missing=3),
        Gap(start=T0 + 25 * HOUR, end=T0 + 30 * HOUR, missing=5),
    ]


async def test_series_are_kept_apart(store: CandleStore) -> None:
    eth = Series("mexc", "ETH/USDT", "1h")
    btc_4h = Series("mexc", "BTC/USDT", "4h")
    await store.upsert(BTC, [candle(0), candle(1)])
    await store.upsert(eth, [candle(0), candle(5)])  # a gap, but in another series
    await store.upsert(btc_4h, [candle(0), candle(4)])  # 4h apart: no gap for 4h

    assert await store.find_gaps(BTC) == []
    assert await store.find_gaps(btc_4h) == []
    assert len(await store.find_gaps(eth)) == 1
    bounds = await store.bounds(BTC)
    assert bounds is not None and bounds.count == 2


async def test_bounds_of_an_empty_series(store: CandleStore) -> None:
    assert await store.bounds(BTC) is None
    assert await store.find_gaps(BTC) == []
