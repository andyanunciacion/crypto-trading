"""The regime detector's rules, each pinned on a hand-built series where the answer is known."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.regime.detector import RegimeDetector, RegimeParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
DETECTOR = RegimeDetector(RegimeParams())
WARMUP = DETECTOR.warmup_candles


def candles(closes: list[float], spread: float = 0.005) -> list[Candle]:
    """Candles closing at `closes`, each with a high and low `spread` above and below."""

    def one(i: int, close: float) -> Candle:
        def price(value: float) -> Decimal:
            return Decimal(str(round(value, 6)))

        return Candle(
            ts=T0 + i * HOUR,
            open=price(closes[i - 1] if i else close),
            high=price(close * (1 + spread)),
            low=price(close * (1 - spread)),
            close=price(close),
            volume=Decimal(1),
        )

    return [one(i, close) for i, close in enumerate(closes)]


def growing(n: int, rate: float, start: float = 100.0) -> list[float]:
    return [start * (1 + rate) ** i for i in range(n)]


def zigzag(n: int) -> list[float]:
    return [100.0 if i % 2 == 0 else 101.0 for i in range(n)]


def wide(candle: Candle, spread: Decimal) -> Candle:
    """The same candle with its high and low pushed `spread` of its close apart."""
    return candle.model_copy(
        update={"high": candle.close * (1 + spread), "low": candle.close * (1 - spread)}
    )


def test_warmup_covers_adx_the_ema_slope_and_the_volatility_history() -> None:
    # The volatility needs the most with the defaults: ATR(14)'s seed fades over 6 x 14
    # candles, then 100 candles of ATR% to average, and the current one: 7 x 14 + 101.
    assert WARMUP == 199
    # A long EMA can need more: 3 x 80 + 10 slope candles + 1.
    assert RegimeDetector(RegimeParams(ema_period=80)).warmup_candles == 251


def test_a_steady_rise_is_trending_up() -> None:
    reading = DETECTOR.detect(candles(growing(WARMUP, 0.01)))

    assert reading.regime == "trending_up"
    assert reading.ts == T0 + (WARMUP - 1) * HOUR
    assert reading.indicators["adx"] == pytest.approx(100.0)
    assert reading.indicators["ema_slope_pct"] > 0
    assert reading.indicators["vol_ratio"] == pytest.approx(1.0, abs=0.01)


def test_a_steady_fall_is_trending_down() -> None:
    reading = DETECTOR.detect(candles(growing(WARMUP, -0.01)))

    assert reading.regime == "trending_down"
    assert reading.indicators["minus_di"] > reading.indicators["plus_di"]
    assert reading.indicators["ema_slope_pct"] < 0


def test_a_tight_zigzag_is_ranging() -> None:
    reading = DETECTOR.detect(candles(zigzag(WARMUP)))

    assert reading.regime == "ranging"
    assert reading.indicators["adx"] < 20


def test_one_wide_candle_after_a_calm_stretch_is_high_volatility() -> None:
    series = candles(zigzag(WARMUP))
    series[-1] = wide(series[-1], Decimal("0.1"))

    reading = DETECTOR.detect(series)

    assert reading.regime == "high_volatility"
    assert reading.indicators["vol_ratio"] >= 1.5


def test_high_volatility_comes_before_a_trend() -> None:
    # A rise whose last candle is a 30% range: still a strong uptrend by ADX and the
    # slope, but the switcher should see a market moving far more than it normally does.
    series = candles(growing(WARMUP, 0.01))
    series[-1] = wide(series[-1], Decimal("0.15"))

    reading = DETECTOR.detect(series)

    assert reading.regime == "high_volatility"
    assert reading.indicators["adx"] >= 25
    assert reading.indicators["plus_di"] > reading.indicators["minus_di"]


def test_between_the_range_and_trend_lines_is_unclear() -> None:
    # A zigzag drifting up: some trend, not much. Put the dead band around its ADX.
    closes = [close * 1.002**i for i, close in enumerate(zigzag(WARMUP))]
    adx = DETECTOR.detect(candles(closes)).indicators["adx"]
    detector = RegimeDetector(RegimeParams(range_adx=adx - 1, trend_adx=adx + 1))

    reading = detector.detect(candles(closes))

    assert reading.regime == "unclear"
    assert "between" in reading.reason


def test_a_trend_whose_direction_lines_disagree_is_unclear() -> None:
    # A long rise, then a sharp fall for 7 candles: the DI lines have turned down, but
    # the 50-candle EMA is still higher than 10 candles ago, and ADX hasn't faded yet.
    closes = growing(WARMUP - 7, 0.01) + growing(8, -0.03, start=100 * 1.01 ** (WARMUP - 8))[1:]
    reading = DETECTOR.detect(candles(closes, spread=0.02))

    assert reading.indicators["adx"] >= 25
    assert reading.indicators["minus_di"] > reading.indicators["plus_di"]
    assert reading.indicators["ema_slope_pct"] > 0
    assert reading.indicators["vol_ratio"] < 1.5
    assert reading.regime == "unclear"
    assert "disagree" in reading.reason


def test_too_few_candles_is_unclear_while_warming_up() -> None:
    reading = DETECTOR.detect(candles(growing(WARMUP - 1, 0.01)))

    assert reading.regime == "unclear"
    assert reading.reason == "warming up"
    assert reading.indicators == {}


def test_decides_on_the_last_warmup_candles_only() -> None:
    # Stateless (D28): whatever came before the window doesn't change the label, so a
    # backtest, a live bot and a bot that just restarted all agree.
    window = growing(WARMUP, 0.01, start=200.0)
    history = zigzag(150) + window
    with_history = candles(history)

    assert DETECTOR.detect(with_history) == DETECTOR.detect(with_history[-WARMUP:])


def test_a_flat_market_reads_as_ranging_without_dividing_by_zero() -> None:
    reading = DETECTOR.detect(candles([100.0] * WARMUP, spread=0.0))

    assert reading.regime == "ranging"
    assert "vol_ratio" not in reading.indicators


def test_params_are_checked() -> None:
    with pytest.raises(ValidationError, match="range_adx"):
        RegimeParams(range_adx=30, trend_adx=25)
    with pytest.raises(ValidationError):
        RegimeParams(adx_period=0)
    with pytest.raises(ValidationError):
        RegimeParams(vol_multiple=1.0)
    with pytest.raises(ValidationError):
        RegimeParams.model_validate({"unknown": 1})
