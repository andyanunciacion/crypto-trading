"""Record the BTC/USDT daily history the regime detector's labeled tests replay.

Run with `uv run python -m engine.regime.record_history`. It overwrites
engine/tests/fixtures/binance/btcusdt_1d_2019_2023.json.gz with Binance's daily candles
from 2019-06-01 to the end of 2023, which hold well-known trends, ranges and crashes
(test_regime_history.py). MEXC can't supply them: it serves about a year of daily
candles. Binance's public market-data host needs no key, and nothing outside the tests
uses Binance, so this downloads the klines directly instead of going through CCXT.
"""

import gzip
import itertools
import json
import ssl
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engine.exchange.models import Candle

# src/engine/regime/record_history.py -> engine/tests/fixtures
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
FIXTURE = FIXTURES_DIR / "binance" / "btcusdt_1d_2019_2023.json.gz"
START = datetime(2019, 6, 1, tzinfo=UTC)
END = datetime(2024, 1, 1, tzinfo=UTC)  # exclusive
KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
PAGE = 1000  # the most Binance returns per request


def fetch() -> list[Candle]:
    # The OS certificate store, as engine.exchange.record trusts it (Avast on a dev PC).
    context = ssl.create_default_context()
    candles: list[Candle] = []
    since = START
    while since < END:
        query = (
            f"symbol=BTCUSDT&interval=1d&limit={PAGE}&startTime={_ms(since)}&endTime={_ms(END) - 1}"
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


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def main() -> None:
    candles = fetch()
    days = (END - START).days
    if len(candles) != days or any(
        b.ts - a.ts != timedelta(days=1) for a, b in itertools.pairwise(candles)
    ):
        raise SystemExit(f"expected {days} consecutive days, got {len(candles)} candles")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps([candle.model_dump(mode="json") for candle in candles], indent=0)
    # mtime=0 so re-recording the same candles gives the same bytes.
    FIXTURE.write_bytes(gzip.compress(body.encode("utf-8"), mtime=0))
    print(f"wrote {len(candles)} candles to {FIXTURE}")


if __name__ == "__main__":
    main()
