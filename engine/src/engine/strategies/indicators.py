"""Indicator math for strategies, in plain floats (D9 allows floats here, and only here).

Hand-rolled rather than from a TA library (D28): each one is a few lines and tested
against hand-worked values.
"""

from collections.abc import Sequence


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
