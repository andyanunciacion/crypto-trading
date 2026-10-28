"""The regime detector on labeled BTC/USDT history: P6-3's done-when.

The fixture is Binance's daily BTC/USDT from 2019-06 to the end of 2023
(`python -m engine.regime.record_history`; MEXC keeps only about a year). Each span
below is a well-known stretch of market history, labeled from what happened rather
than from the detector, which runs with its default (textbook) settings.

ADX takes weeks to rise when a trend starts and to fade when it ends: on daily candles
the 2023 range read as the June rally's uptrend until late July. So trend and range
spans are judged from 2 x `adx_period` candles in (D53). Volatility is read from ATR,
which reacts within a day or two, so crash spans get no allowance. A trend span must
never read as the opposite trend at any point, the lag included.
"""

import gzip
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.exchange.models import Candle
from engine.regime.detector import Regime, RegimeDetector, RegimeParams

FIXTURE = Path(__file__).parent / "fixtures" / "binance" / "btcusdt_1d_2019_2023.json.gz"
PARAMS = RegimeParams()
LAG = timedelta(days=2 * PARAMS.adx_period)
OPPOSITE: dict[Regime, Regime] = {"trending_up": "trending_down", "trending_down": "trending_up"}

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


@pytest.fixture(scope="module")
def labels() -> dict[date, Regime]:
    candles = [
        Candle.model_validate(row) for row in json.loads(gzip.decompress(FIXTURE.read_bytes()))
    ]
    detector = RegimeDetector(PARAMS)
    window = detector.warmup_candles
    return {
        candle.ts.date(): detector.detect(candles[max(0, i + 1 - window) : i + 1]).regime
        for i, candle in enumerate(candles)
    }


@pytest.mark.parametrize(("expected", "start", "end", "what"), SPANS, ids=[s[3] for s in SPANS])
def test_a_labeled_span_reads_as_its_regime(
    labels: dict[date, Regime], expected: Regime, start: date, end: date, what: str
) -> None:
    judged_from = start if expected == "high_volatility" else start + LAG
    whole = Counter(regime for day, regime in labels.items() if start <= day <= end)
    judged = Counter(regime for day, regime in labels.items() if judged_from <= day <= end)
    print(f"{expected} {start}..{end} ({what}): {dict(whole)}; from {judged_from}: {dict(judged)}")

    assert sum(whole.values()) == (end - start).days + 1
    assert judged.most_common(1)[0][0] == expected, f"from {judged_from}: {dict(judged)}"
    if expected in OPPOSITE:
        assert whole[OPPOSITE[expected]] == 0, f"{start}..{end}: {dict(whole)}"
