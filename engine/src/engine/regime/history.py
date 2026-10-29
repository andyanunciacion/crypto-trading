"""Labeled stretches of BTC/USDT history, to judge a regime detector by (D53).

Each span is a well-known stretch of market history, labeled from what happened rather
than from any detector. `test_regime_history.py` checks the detector against them and
the P6-5b study (`study.py`) measures how late each variant recognises them. The
candles come from Binance's daily history (`record_history.py`; MEXC keeps only about a
year).

ADX takes weeks to rise when a trend starts and to fade when it ends, so trend and
range spans are judged from `LAG` (2 x `adx_period` candles) in. Volatility is read
from ATR, which reacts within a day or two, so crash spans get no allowance.
"""

import gzip
import json
from datetime import date, timedelta
from pathlib import Path

from engine.exchange.models import Candle
from shared.regime import Regime, RegimeParams

# src/engine/regime/history.py -> engine/tests/fixtures/binance
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "binance"
BTC_2019_2023 = FIXTURES / "btcusdt_1d_2019_2023.json.gz"

LAG = timedelta(days=2 * RegimeParams().adx_period)

SPANS: list[tuple[Regime, date, date, str]] = [
    ("trending_up", date(2020, 10, 10), date(2020, 12, 15), "11k to 19k, before the 2021 bull run"),
    ("trending_up", date(2023, 10, 16), date(2023, 12, 8), "28k to 43k, on spot ETF hopes"),
    ("trending_down", date(2021, 11, 16), date(2022, 1, 22), "65k to 35k, after the top"),
    ("trending_down", date(2022, 4, 5), date(2022, 5, 8), "46k to 34k, before the Luna crash"),
    ("ranging", date(2020, 5, 20), date(2020, 7, 20), "9k to 10k after the halving"),
    ("ranging", date(2023, 6, 25), date(2023, 8, 14), "29k to 31.5k, the quiet summer"),
    ("high_volatility", date(2020, 3, 9), date(2020, 3, 20), "the COVID crash"),
    ("high_volatility", date(2021, 5, 13), date(2021, 5, 25), "China's mining ban"),
    ("high_volatility", date(2022, 6, 13), date(2022, 6, 20), "Celsius and 3AC"),
]


def load(path: Path) -> list[Candle]:
    """The candles in a gzipped JSON file `record_history` wrote, oldest first."""
    return [Candle.model_validate(row) for row in json.loads(gzip.decompress(path.read_bytes()))]
