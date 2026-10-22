"""Download candle history into the database, then find and fill the gaps in it.

Run inside the stack (Postgres has no host port):
    docker compose run --rm engine python -m engine.marketdata.backfill BTC/USDT 1h --days 365
Add `--check` to only report gaps from the database, without calling the exchange.

Only closed candles are stored. Gaps the exchange can't fill (it has no data for them)
are reported and logged, never filled with made-up candles (D25).
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, get_args

import structlog

from engine.exchange.adapter import ExchangeAdapter
from engine.exchange.models import Candle, Timeframe
from engine.main import configure_logging
from engine.marketdata.store import Bounds, CandleStore, Gap, Series
from engine.marketdata.timeframes import STEP, floor, is_closed
from shared.config import Settings, load_settings
from shared.db.session import make_engine

# MEXC returns at most 500 candles per request, however many are asked for.
PAGE_LIMIT = 500

log = structlog.get_logger()


class CandleSource(Protocol):
    """The part of ExchangeAdapter this module uses, so tests can pass a fake."""

    exchange_id: str

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]: ...


@dataclass(frozen=True)
class SyncResult:
    series: Series
    fetched: int  # candles downloaded and stored in this run
    bounds: Bounds | None  # what's stored now
    gaps: list[Gap]  # what's still missing: the exchange has no data for it


async def download(
    source: CandleSource, store: CandleStore, series: Series, start: datetime, end: datetime
) -> int:
    """Store the candles with open times in [start, end) that had closed by `end`.

    Pages forward through the exchange. A page with nothing in range means the exchange
    has no data there (before its retention limit or the pair's listing, or a long
    outage), so the next page starts where that one would have ended.
    """
    step = STEP[series.timeframe]
    since = start
    stored = 0
    while since < end:
        page = await source.fetch_candles(
            series.symbol, series.timeframe, since=since, limit=PAGE_LIMIT
        )
        # Keeping only ts >= since also guarantees progress: the next page starts after these.
        wanted = [c for c in page if since <= c.ts < end and is_closed(c, series.timeframe, end)]
        if not wanted:
            log.debug("candles.download.empty", series=str(series), since=since.isoformat())
            since += step * PAGE_LIMIT
            continue
        stored += await store.upsert(series, wanted)
        log.debug(
            "candles.download.page",
            series=str(series),
            count=len(wanted),
            first=wanted[0].ts.isoformat(),
            last=wanted[-1].ts.isoformat(),
        )
        since = wanted[-1].ts + step
    return stored


async def sync(
    source: CandleSource,
    store: CandleStore,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime | None = None,
) -> SyncResult:
    """Make the stored series complete from `start` to `end` (default: now).

    Downloads only what's missing: before the first stored candle, the gaps between
    stored candles, and after the last one. Then checks again for gaps.
    """
    end = datetime.now(UTC) if end is None else end
    series = Series(source.exchange_id, symbol, timeframe)
    step = STEP[timeframe]

    bounds = await store.bounds(series)
    if bounds is None:
        ranges = [(start, end)]
    else:
        ranges = [(start, bounds.first)]
        ranges += [(gap.start, gap.end) for gap in await store.find_gaps(series)]
        ranges.append((bounds.last + step, end))

    fetched = 0
    for range_start, range_end in ranges:
        if range_start < range_end:
            fetched += await download(source, store, series, range_start, range_end)

    gaps = await store.find_gaps(series)
    for gap in gaps:
        log.warning(
            "candles.gap_unfillable",
            series=str(series),
            start=gap.start.isoformat(),
            end=gap.end.isoformat(),
            missing=gap.missing,
        )
    result = SyncResult(series, fetched, await store.bounds(series), gaps)
    log.info(
        "candles.sync.done",
        series=str(series),
        fetched=fetched,
        stored=0 if result.bounds is None else result.bounds.count,
        gaps=len(gaps),
    )
    return result


def describe(series: Series, bounds: Bounds | None, gaps: list[Gap]) -> str:
    if bounds is None:
        return f"{series}: no candles stored"
    lines = [
        f"{series}: {bounds.count} candles stored, {bounds.first:%Y-%m-%d %H:%M} to "
        f"{bounds.last:%Y-%m-%d %H:%M} UTC, {len(gaps)} gaps"
    ]
    lines += [
        f"  gap: {gap.missing} missing from {gap.start:%Y-%m-%d %H:%M} "
        f"up to {gap.end:%Y-%m-%d %H:%M} UTC"
        for gap in gaps
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace, settings: Settings) -> bool:
    """Returns True when every series is gap-free."""
    db = make_engine(settings.database_url)
    store = CandleStore(db)
    complete = True
    try:
        if args.check:
            for timeframe in args.timeframes:
                series = Series(args.exchange, args.symbol, timeframe)
                gaps = await store.find_gaps(series)
                print(describe(series, await store.bounds(series), gaps))
                complete = complete and not gaps
        else:
            start = datetime.now(UTC) - timedelta(days=args.days)
            async with ExchangeAdapter(args.exchange) as adapter:
                for timeframe in args.timeframes:
                    result = await sync(
                        adapter, store, args.symbol, timeframe, floor(start, timeframe)
                    )
                    print(f"{result.series}: downloaded {result.fetched} candles")
                    print(describe(result.series, result.bounds, result.gaps))
                    complete = complete and not result.gaps
    finally:
        await db.dispose()
    return complete


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("symbol", help="CCXT format, e.g. BTC/USDT")
    parser.add_argument("timeframes", nargs="+", choices=get_args(Timeframe), metavar="timeframe")
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="how far back to download (default 365). MEXC keeps about 30 days of 1m "
        "and 360 days of 5m/15m; older requests just come back empty.",
    )
    parser.add_argument("--exchange", default="mexc")
    parser.add_argument("--check", action="store_true", help="only report gaps; no downloads")
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level)
    # Exit code 1 when gaps remain, so scripts can tell.
    raise SystemExit(0 if asyncio.run(run(args, settings)) else 1)


if __name__ == "__main__":
    main()
