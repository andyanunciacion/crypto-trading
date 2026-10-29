"""The regime detector on labeled BTC/USDT history: P6-3's done-when.

The fixture is Binance's daily BTC/USDT from 2019-06 to the end of 2023
(`python -m engine.regime.record_history`; MEXC keeps only about a year). The spans
(`engine/regime/history.py`) are well-known stretches of market history, labeled from
what happened rather than from the detector, which runs with its default (textbook)
settings.

ADX takes weeks to rise when a trend starts and to fade when it ends: on daily candles
the 2023 range read as the June rally's uptrend until late July. So trend and range
spans are judged from 2 x `adx_period` candles in (D53). Volatility is read from ATR,
which reacts within a day or two, so crash spans get no allowance. A trend span must
never read as the opposite trend at any point, the lag included.
"""

from collections import Counter
from datetime import date

import pytest

from engine.regime.detector import Regime, RegimeDetector, RegimeParams
from engine.regime.history import BTC_2019_2023, LAG, SPANS, load

OPPOSITE: dict[Regime, Regime] = {"trending_up": "trending_down", "trending_down": "trending_up"}


@pytest.fixture(scope="module")
def labels() -> dict[date, Regime]:
    candles = load(BTC_2019_2023)
    detector = RegimeDetector(RegimeParams())
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
