"""Indicator math for strategies, in plain floats (D9 allows floats here, and only here).

Hand-rolled rather than from a TA library (D28): each one is a few lines and tested
against hand-worked values.
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
