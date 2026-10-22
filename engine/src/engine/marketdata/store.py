"""Reads and writes candles in the `candles` hypertable."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.exchange.models import Candle, Timeframe
from engine.marketdata.timeframes import STEP
from shared.db.models import CandleRow

# The gap check: each stored candle next to the one after it, keeping the pairs that are
# more than one step apart. Only looks between stored candles, so history from before
# the first one (the exchange's retention limit, the pair's listing date) isn't a gap.
GAP_QUERY = text("""
    SELECT ts, next_ts
    FROM (
        SELECT ts, lead(ts) OVER (ORDER BY ts) AS next_ts
        FROM candles
        WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
    ) AS series
    WHERE next_ts - ts > :step
    ORDER BY ts
""")


@dataclass(frozen=True)
class Series:
    """One candle series: an exchange, a symbol and a timeframe."""

    exchange: str
    symbol: str
    timeframe: Timeframe

    def __str__(self) -> str:
        return f"{self.exchange} {self.symbol} {self.timeframe}"


@dataclass(frozen=True)
class Gap:
    """Missing candles: open times from `start` up to, not including, `end`."""

    start: datetime  # the first missing candle
    end: datetime  # the next stored candle
    missing: int


@dataclass(frozen=True)
class Bounds:
    first: datetime
    last: datetime
    count: int


class CandleStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(self, series: Series, candles: list[Candle]) -> int:
        """Insert candles, or overwrite the stored ones with the same open time.

        Idempotent: storing the same candles twice leaves the same rows.
        """
        if not candles:
            return 0
        rows = [
            {
                "exchange": series.exchange,
                "symbol": series.symbol,
                "timeframe": series.timeframe,
                **candle.model_dump(),
            }
            for candle in candles
        ]
        # ON CONFLICT DO UPDATE: Postgres's "upsert". A row that already exists takes the
        # new values instead of raising a duplicate-key error.
        stmt = insert(CandleRow)
        stmt = stmt.on_conflict_do_update(
            constraint="pk_candles",
            set_={name: stmt.excluded[name] for name in ("open", "high", "low", "close", "volume")},
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt, rows)
        return len(rows)

    async def bounds(self, series: Series) -> Bounds | None:
        """The first and last stored candle and how many there are; None if there are none."""
        query = select(func.min(CandleRow.ts), func.max(CandleRow.ts), func.count()).where(
            *_matches(series)
        )
        async with self._engine.connect() as conn:
            first, last, count = (await conn.execute(query)).one()
        if count == 0:
            return None
        return Bounds(first=first, last=last, count=count)

    async def find_gaps(self, series: Series) -> list[Gap]:
        step = STEP[series.timeframe]
        params = {
            "exchange": series.exchange,
            "symbol": series.symbol,
            "timeframe": series.timeframe,
            "step": step,
        }
        async with self._engine.connect() as conn:
            rows = (await conn.execute(GAP_QUERY, params)).all()
        return [
            Gap(start=ts + step, end=next_ts, missing=(next_ts - ts) // step - 1)
            for ts, next_ts in rows
        ]

    async def candles(self, series: Series, start: datetime, end: datetime) -> list[Candle]:
        """Stored candles with open times from `start` up to, not including, `end`, oldest first."""
        query = (
            select(
                CandleRow.ts,
                CandleRow.open,
                CandleRow.high,
                CandleRow.low,
                CandleRow.close,
                CandleRow.volume,
            )
            .where(*_matches(series), CandleRow.ts >= start, CandleRow.ts < end)
            .order_by(CandleRow.ts)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [Candle.model_validate(dict(row)) for row in rows]


def _matches(series: Series) -> tuple[ColumnElement[bool], ...]:
    return (
        CandleRow.exchange == series.exchange,
        CandleRow.symbol == series.symbol,
        CandleRow.timeframe == series.timeframe,
    )
