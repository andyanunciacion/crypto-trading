"""Record the daily history the regime detector's labeled tests and P6-5b's study replay.

Run with `uv run python -m engine.regime.record_history`. With no options it overwrites
engine/tests/fixtures/binance/btcusdt_1d_2019_2023.json.gz with Binance's daily
BTC/USDT candles from 2019-06-01 to the end of 2023, which hold well-known trends,
ranges and crashes (test_regime_history.py). `--symbol`, `--start` and `--end` record
another coin or other years (the study's holdouts), named after them, e.g.
`--symbol ETH/USDT` writes ethusdt_1d_2019_2023.json.gz. MEXC can't supply them: it
serves about a year of daily candles. Binance's public market-data host needs no key,
and nothing outside the tests uses Binance, so this downloads the klines directly
instead of going through CCXT.
"""

import argparse
import gzip
import itertools
import json
import ssl
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from engine.exchange.models import Candle

# src/engine/regime/record_history.py -> engine/tests/fixtures
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
SYMBOL = "BTC/USDT"
START = date(2019, 6, 1)
END = date(2024, 1, 1)  # exclusive
KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
PAGE = 1000  # the most Binance returns per request


def fixture(symbol: str, start: date, end: date) -> Path:
    """Where the candles of `symbol` from `start` up to `end` (exclusive) are kept."""
    pair = symbol.replace("/", "").lower()
    last = end - timedelta(days=1)
    return FIXTURES_DIR / "binance" / f"{pair}_1d_{start.year}_{last.year}.json.gz"


def fetch(symbol: str, start: date, end: date) -> list[Candle]:
    # The OS certificate store, as engine.exchange.record trusts it (Avast on a dev PC).
    context = ssl.create_default_context()
    pair = symbol.replace("/", "")
    until = _at(end)
    candles: list[Candle] = []
    since = _at(start)
    while since < until:
        query = (
            f"symbol={pair}&interval=1d&limit={PAGE}"
            f"&startTime={_ms(since)}&endTime={_ms(until) - 1}"
        )
        with urllib.request.urlopen(f"{KLINES_URL}?{query}", context=context, timeout=30) as reply:
            rows = json.load(reply)
        if not rows:
            break
        # Each row: open time (ms), open, high, low, close, volume, then fields we don't need.
        candles += [
            Candle(
                ts=datetime.fromtimestamp(row[0] / 1000, UTC),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
            )
            for row in rows
        ]
        since = candles[-1].ts + timedelta(days=1)
    return candles


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default=SYMBOL, help=f"default {SYMBOL}")
    parser.add_argument("--start", type=date.fromisoformat, default=START, help=f"default {START}")
    parser.add_argument(
        "--end", type=date.fromisoformat, default=END, help=f"exclusive; default {END}"
    )
    args = parser.parse_args()
    symbol: str = args.symbol
    start: date = args.start
    end: date = args.end
    candles = fetch(symbol, start, end)
    days = (end - start).days
    if len(candles) != days or any(
        b.ts - a.ts != timedelta(days=1) for a, b in itertools.pairwise(candles)
    ):
        raise SystemExit(f"expected {days} consecutive days, got {len(candles)} candles")
    out = fixture(symbol, start, end)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps([candle.model_dump(mode="json") for candle in candles], indent=0)
    # mtime=0 so re-recording the same candles gives the same bytes.
    out.write_bytes(gzip.compress(body.encode("utf-8"), mtime=0))
    print(f"wrote {len(candles)} candles to {out}")


if __name__ == "__main__":
    main()
