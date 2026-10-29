"""P6-5b's study (engine/regime/study.py, D56): its variants and its keep rule.

The study's numbers are in D56; these pin what they rest on: the study's v1 is the
detector P6-4 measured, the matched efficiency-ratio levels are D56's, each variant
reads as its rule says, and the keep rule counts cells as D56 says.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from engine.exchange.models import Candle
from engine.regime.detector import RegimeDetector
from engine.regime.history import BTC_2019_2023, load
from engine.regime.study import (
    EMA,
    EMA_STATE,
    EfficiencyExit,
    EfficiencyStrength,
    ErLevels,
    Memo,
    _beats,
    _kept,
    er_levels,
    labels,
    settings,
)
from engine.regime.switcher import AutoSwitcher, same_config
from engine.strategies.base import Position, Strategy
from shared.regime import RegimeParams

DAY = timedelta(days=1)
LEVELS = ErLevels(trend=0.15, range=0.05)


@pytest.fixture(scope="module")
def btc() -> list[Candle]:
    return load(BTC_2019_2023)


def switches(candles: list[Candle], detector: RegimeDetector, min_hold: int) -> int:
    """How many times a bot starting on EMA crossover switches, as test_auto_switch_replay
    feeds it, with P6-4's mapping."""
    auto = AutoSwitcher(settings(min_hold, "keep"))
    auto.detector = detector
    current: Strategy[Any] = EMA
    count = 0
    for i in range(len(candles)):
        check = auto.check(candles[max(0, i + 1 - auto.window) : i + 1], current, DAY)
        if check.switch is not None:
            count += 1
            current = check.switch.target
    return count


def test_v1_is_the_detector_p6_4_measured(btc: list[Candle]) -> None:
    # D54 recorded 57, 41 and 34 switches for a hold of 1, 3 and 5.
    memo = Memo(RegimeDetector(RegimeParams()))

    assert [switches(btc, memo, hold) for hold in (1, 3, 5)] == [57, 41, 34]


def test_the_matched_levels_are_d56s(btc: list[Candle]) -> None:
    assert er_levels(btc) == LEVELS


def test_the_er_variant_decides_on_the_ratio_alone(btc: list[Candle]) -> None:
    detector = EfficiencyStrength(RegimeParams(), LEVELS)
    w = detector.warmup_candles
    seen = set()
    for end in range(w, len(btc) + 1):
        reading = detector.detect(btc[end - w : end])
        er = reading.indicators["er"]
        seen.add(reading.regime)
        if reading.regime == "high_volatility":
            continue
        if er >= LEVELS.trend:
            assert reading.regime in ("trending_up", "trending_down", "unclear")
        elif er < LEVELS.range:
            assert reading.regime == "ranging"
        else:
            assert reading.regime == "unclear"
    assert seen == {"trending_up", "trending_down", "ranging", "high_volatility", "unclear"}


def test_the_er_exit_turns_a_trend_unclear_only_when_the_ratio_is_low(btc: list[Candle]) -> None:
    levels = ErLevels(trend=0.5, range=0.5)
    v1 = labels(btc, RegimeDetector(RegimeParams()))
    exit_ = EfficiencyExit(RegimeParams(), levels)
    w = exit_.warmup_candles
    readings = [exit_.detect(btc[end - w : end]) for end in range(w, len(btc) + 1)]

    changed = 0
    for (_, before), after in zip(v1, readings, strict=True):
        if before in ("trending_up", "trending_down") and after.indicators["er"] < levels.range:
            assert after.regime == "unclear"
            changed += 1
        else:
            assert after.regime == before
    assert changed > 0


def test_memo_gives_the_inner_readings(btc: list[Candle]) -> None:
    inner = RegimeDetector(RegimeParams())
    memo = Memo(inner)
    w = inner.warmup_candles
    for end in (w - 1, w, w + 50):
        window = btc[max(0, end - w) : end]
        assert memo.detect(window) == inner.detect(window)
        assert memo.detect(window) is memo.detect(window)  # the second read is remembered
    assert memo.warmup_candles == w


def test_the_state_entry_buys_while_fast_is_above_slow() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    def candles(closes: list[float]) -> list[Candle]:
        return [
            Candle(ts=t0 + i * DAY, open=p, high=p, low=p, close=p, volume=Decimal(1))
            for i, p in enumerate(Decimal(str(round(c, 4))) for c in closes)
        ]

    rising = candles([100 * 1.01**i for i in range(EMA_STATE.warmup_candles + 3)])
    falling = candles([100 * 0.99**i for i in range(EMA_STATE.warmup_candles + 3)])
    held = Position(amount=Decimal(1), avg_entry=Decimal(100))

    # Mid-rise, no cross on the last candle: the state entry buys, the cross entry waits.
    assert EMA_STATE.on_candle(rising, None).action == "buy"
    assert EMA.on_candle(rising, None).action == "hold"
    assert EMA_STATE.on_candle(rising, held).action == "hold"
    assert EMA_STATE.on_candle(falling, None).action == "hold"
    # Its own name, so a switch to it from EMA crossover is a switch.
    assert not same_config(EMA_STATE, EMA)


def test_the_keep_rule_needs_six_of_nine_and_two_of_three_on_each_holdout() -> None:
    returns = [Decimal(3), Decimal(1), Decimal(2)]
    assert _beats(returns, [Decimal(2)] * 3) == (1, 3)  # a tie isn't a win
    assert _kept([(6, 9), (2, 3), (2, 3)])
    assert not _kept([(5, 9), (3, 3), (3, 3)])
    assert not _kept([(9, 9), (3, 3), (1, 3)])
