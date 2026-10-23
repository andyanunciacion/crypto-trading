"""The downloader: paging, closed candles only, skipping empty stretches, filling gaps.

A fake exchange serves a synthetic hourly series. The first tests use an in-memory
store and need no database; the `sync` tests use a real TimescaleDB (root conftest.py).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_args

import pytest
from sqlalchemy import URL, text

from engine.exchange.models import Candle, Timeframe
from engine.marketdata.backfill import download, sync
from engine.marketdata.candle_stream import PAGE_LIMIT
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP, floor
from shared.db.models import TIMEFRAMES
from shared.db.session import make_engine

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
SERIES = Series("mexc", "BTC/USDT", "1h")


def hourly(first: int, count: int, *, missing: range = range(0)) -> list[Candle]:
    """Hourly candles for hours first..first+count-1 after T0, without the `missing` hours."""
    return [
        Candle(
            ts=T0 + i * HOUR,
            open=Decimal(100 + i),
            high=Decimal(101 + i),
            low=Decimal(99 + i),
            close=Decimal("100.5") + i,
            volume=Decimal("0.123"),
        )
        for i in range(first, first + count)
        if i not in missing
    ]


@dataclass
class FakeExchange:
    """Answers like MEXC through CCXT: candles from `since` for at most `limit` steps."""

    candles: list[Candle]
    exchange_id: str = "mexc"
    calls: list[datetime] = field(default_factory=list)

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        assert since is not None and limit is not None
        self.calls.append(since)
        window_end = since + STEP[timeframe] * limit
        return [c for c in self.candles if since <= c.ts < window_end]


class MemoryStore(CandleStore):
    """Keeps upserted candles in a dict, keyed like the table's primary key."""

    def __init__(self) -> None:
        self.rows: dict[datetime, Candle] = {}

    async def upsert(self, series: Series, candles: list[Candle]) -> int:
        self.rows.update((c.ts, c) for c in candles)
        return len(candles)


def test_timeframes_match_the_database_check() -> None:
    assert get_args(Timeframe) == TIMEFRAMES
    assert set(STEP) == set(TIMEFRAMES)


def test_floor_finds_the_candle_open_time() -> None:
    ts = datetime(2026, 10, 22, 14, 47, 31, tzinfo=UTC)

    assert floor(ts, "1m") == datetime(2026, 10, 22, 14, 47, tzinfo=UTC)
    assert floor(ts, "15m") == datetime(2026, 10, 22, 14, 45, tzinfo=UTC)
    assert floor(ts, "1h") == datetime(2026, 10, 22, 14, tzinfo=UTC)
    assert floor(ts, "4h") == datetime(2026, 10, 22, 12, tzinfo=UTC)
    assert floor(ts, "1d") == datetime(2026, 10, 22, tzinfo=UTC)
    assert floor(datetime(2026, 10, 22, 12, tzinfo=UTC), "4h") == datetime(
        2026, 10, 22, 12, tzinfo=UTC
    )


# --- download (no database) -------------------------------------------------------------


async def test_download_pages_through_the_range() -> None:
    exchange = FakeExchange(hourly(0, 1200))
    store = MemoryStore()

    stored = await download(exchange, store, SERIES, T0, T0 + 1200 * HOUR)

    assert stored == 1200
    assert sorted(store.rows) == [c.ts for c in exchange.candles]
    # 500 + 500 + 200: each page starts right after the last candle of the one before.
    assert exchange.calls == [T0, T0 + PAGE_LIMIT * HOUR, T0 + 2 * PAGE_LIMIT * HOUR]


async def test_download_leaves_out_the_open_candle() -> None:
    exchange = FakeExchange(hourly(0, 10))
    store = MemoryStore()
    end = T0 + 9 * HOUR + timedelta(minutes=30)  # "now" is halfway through hour 9

    await download(exchange, store, SERIES, T0, end)

    assert sorted(store.rows) == [T0 + i * HOUR for i in range(9)]


async def test_download_skips_stretches_the_exchange_has_no_data_for() -> None:
    # Like asking for a year of 1m candles when the exchange keeps only 30 days.
    exchange = FakeExchange(hourly(1700, 50))
    store = MemoryStore()

    stored = await download(exchange, store, SERIES, T0, T0 + 1750 * HOUR)

    assert stored == 50
    assert exchange.calls[:4] == [T0 + i * PAGE_LIMIT * HOUR for i in range(4)]


async def test_download_ends_even_if_the_exchange_ignores_since() -> None:
    class StuckExchange(FakeExchange):
        async def fetch_candles(
            self,
            symbol: str,
            timeframe: Timeframe,
            since: datetime | None = None,
            limit: int | None = None,
        ) -> list[Candle]:
            assert since is not None
            self.calls.append(since)
            return self.candles[:5]  # always the same old page

    exchange = StuckExchange(hourly(0, 5))

    await download(exchange, MemoryStore(), SERIES, T0, T0 + 2000 * HOUR)

    assert len(exchange.calls) <= 5


# --- sync (real database) ---------------------------------------------------------------


@pytest.fixture
async def store(db_url: URL) -> AsyncIterator[CandleStore]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE candles"))
    yield CandleStore(engine)
    await engine.dispose()


async def test_sync_downloads_everything_into_an_empty_store(store: CandleStore) -> None:
    exchange = FakeExchange(hourly(0, 1000))

    result = await sync(exchange, store, "BTC/USDT", "1h", T0, T0 + 1000 * HOUR)

    assert result.series == SERIES
    assert result.fetched == 1000
    assert result.bounds is not None
    assert (result.bounds.first, result.bounds.last, result.bounds.count) == (
        T0,
        T0 + 999 * HOUR,
        1000,
    )
    assert result.gaps == []


async def test_sync_fetches_only_what_is_missing(store: CandleStore) -> None:
    # Stored: hours 100-199 with 150-159 missing. The exchange has hours 0-299.
    await store.upsert(SERIES, hourly(100, 100, missing=range(150, 160)))
    exchange = FakeExchange(hourly(0, 300))

    result = await sync(exchange, store, "BTC/USDT", "1h", T0, T0 + 300 * HOUR)

    assert result.fetched == 100 + 10 + 100  # before, the gap, after
    assert exchange.calls == [T0, T0 + 150 * HOUR, T0 + 200 * HOUR]
    assert result.gaps == []
    assert result.bounds is not None and result.bounds.count == 300


async def test_sync_reports_gaps_the_exchange_cannot_fill(store: CandleStore) -> None:
    # The exchange itself has no candles for hours 40-44 (say, maintenance).
    exchange = FakeExchange(hourly(0, 100, missing=range(40, 45)))

    result = await sync(exchange, store, "BTC/USDT", "1h", T0, T0 + 100 * HOUR)

    assert [(g.start, g.end, g.missing) for g in result.gaps] == [
        (T0 + 40 * HOUR, T0 + 45 * HOUR, 5)
    ]
    # Nothing made up to fill it.
    assert await store.candles(SERIES, T0 + 40 * HOUR, T0 + 45 * HOUR) == []


async def test_a_second_sync_downloads_nothing_new(store: CandleStore) -> None:
    exchange = FakeExchange(hourly(0, 100))
    end = T0 + 100 * HOUR
    await sync(exchange, store, "BTC/USDT", "1h", T0, end)
    exchange.calls.clear()

    result = await sync(exchange, store, "BTC/USDT", "1h", T0, end)

    assert result.fetched == 0
    assert exchange.calls == []
