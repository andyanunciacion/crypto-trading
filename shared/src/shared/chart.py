"""What the Market chart draws for each strategy: its indicators, as the strategy saw them (D46).

Strategies decide on exactly their last `warmup_candles` candles (D28), so an EMA on
the chart is worked out the same way: each candle's point over its own warm-up window,
not one EMA over the whole chart (whose start-up seed would make it drift slightly).
A candle with too little history before it, or a gap in its window, has no point,
just as the strategy holds there ("warming up", or the SignalRunner waiting out a gap).

The line names are the keys of the strategy's `Signal.indicators`, and
engine/tests/test_chart_parity.py checks every registered strategy's lines equal its
signals' values at every candle, so the chart can't drift from what a bot decided on.
The engine owns the strategies; this module is in `shared` so the API can draw them.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from shared.indicators import bollinger, ema, rsi

# "price" lines share the candles' scale; "oscillator" ones (RSI, 0-100) get a pane below.
Pane = Literal["price", "oscillator"]


class UnknownStrategy(ValueError):
    pass


@dataclass(frozen=True)
class ChartLines:
    warmup: int  # the strategy's warmup_candles
    pane: Pane
    names: tuple[str, ...]
    guides: dict[str, float]  # flat levels drawn across the pane, e.g. RSI's oversold
    at: Callable[[Sequence[float]], dict[str, float]]  # a window's closes -> each line's value


class _Params(BaseModel):
    # Only what the lines need; the engine validated the rest when the config was stored.
    model_config = ConfigDict(extra="ignore", frozen=True)


class _EmaCrossover(_Params):
    fast: int
    slow: int


class _BollingerBreakout(_Params):
    period: int
    width: float


class _RsiMeanReversion(_Params):
    period: int
    oversold: float
    overbought: float


def _ema_crossover(params: Mapping[str, Any]) -> ChartLines:
    p = _EmaCrossover.model_validate(params)
    return ChartLines(
        warmup=3 * p.slow + 1,
        pane="price",
        names=("fast_ema", "slow_ema"),
        guides={},
        at=lambda closes: {
            "fast_ema": ema(closes, p.fast)[-1],
            "slow_ema": ema(closes, p.slow)[-1],
        },
    )


def _bollinger_breakout(params: Mapping[str, Any]) -> ChartLines:
    p = _BollingerBreakout.model_validate(params)

    def at(closes: Sequence[float]) -> dict[str, float]:
        bands = bollinger(closes, p.period, p.width)[-1]
        return {"upper": bands.upper, "middle": bands.middle, "lower": bands.lower}

    return ChartLines(
        warmup=p.period + 1, pane="price", names=("upper", "middle", "lower"), guides={}, at=at
    )


def _rsi_mean_reversion(params: Mapping[str, Any]) -> ChartLines:
    p = _RsiMeanReversion.model_validate(params)
    return ChartLines(
        warmup=6 * p.period + 1,
        pane="oscillator",
        names=("rsi",),
        guides={"oversold": p.oversold, "overbought": p.overbought},
        at=lambda closes: {"rsi": rsi(closes, p.period)[-1]},
    )


def _stay_flat(params: Mapping[str, Any]) -> ChartLines:
    # No indicators: it only sells what it holds (P6-4).
    return ChartLines(warmup=1, pane="price", names=(), guides={}, at=lambda closes: {})


_LINES: dict[str, Callable[[Mapping[str, Any]], ChartLines]] = {
    "ema_crossover": _ema_crossover,
    "bollinger_breakout": _bollinger_breakout,
    "rsi_mean_reversion": _rsi_mean_reversion,
    "stay_flat": _stay_flat,
}


def chart_lines(strategy: str, params: Mapping[str, Any]) -> ChartLines:
    """A stored strategy config's lines. Raises `UnknownStrategy`, or `ValueError` for
    params the lines can't read."""
    if strategy not in _LINES:
        raise UnknownStrategy(f"no chart lines for strategy {strategy!r}")
    return _LINES[strategy](params)


def line_points(
    lines: ChartLines,
    ts: Sequence[datetime],
    closes: Sequence[float],
    step: timedelta,
    *,
    first: int = 0,
) -> list[dict[str, float] | None]:
    """Each candle's values from index `first` on, over its own warm-up window.

    `ts` and `closes` are the candles oldest first, with enough before `first` for its
    window. None where the window is short or has a gap.
    """
    points: list[dict[str, float] | None] = []
    for end in range(first + 1, len(closes) + 1):
        start = end - lines.warmup
        if start < 0 or ts[end - 1] - ts[start] != (lines.warmup - 1) * step:
            points.append(None)
        else:
            points.append(lines.at(closes[start:end]))
    return points
