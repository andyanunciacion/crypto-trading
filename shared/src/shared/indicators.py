"""Indicator math for strategies, in plain floats (D9 allows floats here, and only here).

Hand-rolled rather than from a TA library (D28): each one is a few lines and tested
against hand-worked values. In `shared` so the dashboard's chart draws the strategies'
indicators with the same code (chart.py, D46).
"""

import itertools
import math
from collections.abc import Sequence
from typing import NamedTuple


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average.

    The first value is the simple average of the first `period` values (the usual
    seed); each later one moves a fraction alpha = 2 / (period + 1) of the way toward
    the next value. `result[i]` belongs to `values[i + period - 1]`, so there are
    `len(values) - period + 1` results, or none if there are fewer than `period` values.
    """
    if period < 1:
        raise ValueError(f"EMA period must be at least 1, got {period}")
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    current = sum(values[:period]) / period
    result = [current]
    for value in values[period:]:
        current += alpha * (value - current)
        result.append(current)
    return result


def rsi(values: Sequence[float], period: int) -> list[float]:
    """Relative Strength Index, 0 to 100, with Wilder's smoothing.

    Works on the changes between values. The first average gain and loss are simple
    averages of the first `period` changes; each later change moves them a fraction
    1 / period of the way toward it. RSI = 100 x gain / (gain + loss): 100 with no
    losses, 0 with no gains, and 50 with neither (a flat market is neutral, not
    oversold). `result[i]` belongs to `values[i + period]`, so there are
    `len(values) - period` results, or none if there are `period` values or fewer.
    """
    if period < 1:
        raise ValueError(f"RSI period must be at least 1, got {period}")
    if len(values) <= period:
        return []
    changes = [b - a for a, b in itertools.pairwise(values)]
    gain = sum(max(change, 0.0) for change in changes[:period]) / period
    loss = sum(max(-change, 0.0) for change in changes[:period]) / period
    result = [_rsi(gain, loss)]
    for change in changes[period:]:
        gain += (max(change, 0.0) - gain) / period
        loss += (max(-change, 0.0) - loss) / period
        result.append(_rsi(gain, loss))
    return result


def _rsi(gain: float, loss: float) -> float:
    total = gain + loss
    return 50.0 if total == 0 else 100 * gain / total


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> list[float]:
    """Average True Range, Wilder's: how far the price typically moves in one candle.

    A candle's true range is its high minus its low, widened to the previous close when
    the price gapped past it: max(high - low, |high - prev close|, |low - prev close|).
    The first ATR is the simple average of the first `period` true ranges; each later
    one moves 1 / period of the way toward the next, as in `rsi`. True ranges start at
    the second candle, so `result[i]` belongs to candle `i + period`: there are
    `len(closes) - period` results, or none with `period` candles or fewer.
    """
    if period < 1:
        raise ValueError(f"ATR period must be at least 1, got {period}")
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("ATR needs highs, lows and closes of the same length")
    if len(closes) <= period:
        return []
    ranges = [
        max(high - low, abs(high - prev_close), abs(low - prev_close))
        for high, low, prev_close in zip(highs[1:], lows[1:], closes[:-1], strict=True)
    ]
    current = sum(ranges[:period]) / period
    result = [current]
    for true_range in ranges[period:]:
        current += (true_range - current) / period
        result.append(current)
    return result


class Directional(NamedTuple):
    adx: float
    plus_di: float
    minus_di: float


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> list[Directional]:
    """Average Directional Index, Wilder's: how strong a trend is, and which way.

    From the second candle on, the up-move is the high minus the previous high and the
    down-move the previous low minus the low; the bigger one counts as +DM or -DM if
    it's positive (an inside or outside candle can count as neither). +DM, -DM and the
    true range are smoothed as in `atr`, and +DI / -DI = 100 x smoothed DM / smoothed
    TR: the share of the typical move that went up or down. DX = 100 x |+DI - -DI| /
    (+DI + -DI) says how one-sided that was, and ADX smooths DX the same way, seeded
    with the average of the first `period` DX values. ADX is 0 to 100 and has no
    direction; the DI lines give that. A flat market reads 0 for all three.

    DX starts at candle `period`, so `result[i]` belongs to candle `i + 2 * period - 1`:
    there are `len(closes) - 2 * period + 1` results, or none with fewer candles.
    """
    if period < 1:
        raise ValueError(f"ADX period must be at least 1, got {period}")
    if not len(highs) == len(lows) == len(closes):
        raise ValueError("ADX needs highs, lows and closes of the same length")
    if len(closes) < 2 * period:
        return []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    ranges: list[float] = []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        prev_close = closes[i - 1]
        ranges.append(
            max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
        )

    true_range = sum(ranges[:period]) / period
    plus = sum(plus_dm[:period]) / period
    minus = sum(minus_dm[:period]) / period
    lines = [_di(plus, minus, true_range)]
    for tr, p, m in zip(ranges[period:], plus_dm[period:], minus_dm[period:], strict=True):
        true_range += (tr - true_range) / period
        plus += (p - plus) / period
        minus += (m - minus) / period
        lines.append(_di(plus, minus, true_range))

    dx = [_dx(plus_di, minus_di) for plus_di, minus_di in lines]
    current = sum(dx[:period]) / period
    result = [Directional(current, *lines[period - 1])]
    for value, (plus_di, minus_di) in zip(dx[period:], lines[period:], strict=True):
        current += (value - current) / period
        result.append(Directional(current, plus_di, minus_di))
    return result


def _di(plus: float, minus: float, true_range: float) -> tuple[float, float]:
    if true_range == 0:
        return 0.0, 0.0
    return 100 * plus / true_range, 100 * minus / true_range


def _dx(plus_di: float, minus_di: float) -> float:
    total = plus_di + minus_di
    return 0.0 if total == 0 else 100 * abs(plus_di - minus_di) / total


class Bands(NamedTuple):
    lower: float
    middle: float
    upper: float


def bollinger(values: Sequence[float], period: int, width: float) -> list[Bands]:
    """Bollinger Bands: the simple moving average, plus and minus `width` standard deviations.

    Each band covers the `period` values ending at the one it belongs to, with the
    population standard deviation (dividing by `period`), as Bollinger defined it.
    `result[i]` belongs to `values[i + period - 1]`, so there are
    `len(values) - period + 1` results, or none if there are fewer than `period` values.
    """
    if period < 1:
        raise ValueError(f"Bollinger period must be at least 1, got {period}")
    result = []
    for end in range(period, len(values) + 1):
        window = values[end - period : end]
        middle = sum(window) / period
        deviation = math.sqrt(sum((value - middle) ** 2 for value in window) / period)
        result.append(Bands(middle - width * deviation, middle, middle + width * deviation))
    return result
