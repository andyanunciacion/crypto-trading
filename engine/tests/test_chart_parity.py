"""The Market chart's lines are the strategies' own indicator values (D46).

shared/chart.py draws each strategy's indicators for the dashboard, outside the engine.
For every registered strategy, with its default params and with others, the chart's
point at each candle must equal the `indicators` of the signal the strategy gives on
that candle, exactly: the same code over the same window.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from engine.exchange.models import Candle
from engine.strategies.grid import Grid, GridParams
from engine.strategies.registry import STRATEGIES
from shared.chart import chart_lines, line_points

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def wandering(n: int) -> list[Candle]:
    """A random walk with uneven volume, fixed seed: crosses, breakouts and RSI swings."""
    rng = random.Random(46)
    price = 100.0
    out = []
    for i in range(n):
        price = max(1.0, price * (1 + rng.gauss(0, 0.02)))
        close = Decimal(f"{price:.2f}")
        out.append(
            Candle(
                ts=T0 + i * HOUR,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal(f"{rng.uniform(1, 10):.3f}"),
            )
        )
    return out


PARAMS: list[tuple[str, dict[str, Any]]] = [
    # Each strategy's defaults, where it has them for every param.
    *(
        (name, {})
        for name, cls in STRATEGIES.items()
        if not any(f.is_required() for f in cls.params_model.model_fields.values())
    ),
    ("ema_crossover", {"fast": 3, "slow": 7}),
    ("bollinger_breakout", {"period": 5, "width": 1.0, "volume_factor": 0}),
    ("rsi_mean_reversion", {"period": 5, "oversold": 40, "overbought": 60}),
    ("grid", {"lower": "90", "upper": "110"}),
    ("dca", {"interval": 6, "dip_percent": "5", "take_profit_percent": "10"}),
]


def test_every_strategy_is_charted() -> None:
    assert {name for name, _ in PARAMS} == set(STRATEGIES)


def test_the_grids_lines_are_drawn_where_it_trades() -> None:
    grid = Grid(GridParams(lower=Decimal(90), upper=Decimal(110), grids=4))
    lines = chart_lines("grid", grid.params.model_dump(mode="json"))
    assert list(lines.guides.values()) == [float(line) for line in grid.lines()]
    assert lines.pane == "price"


@pytest.mark.parametrize(("name", "params"), PARAMS)
def test_the_charts_lines_equal_the_signals_indicators(name: str, params: dict[str, Any]) -> None:
    cls = STRATEGIES[name]
    strategy = cls(cls.params_model.model_validate(params))
    lines = chart_lines(name, strategy.params.model_dump(mode="json"))
    assert lines.warmup == strategy.warmup_candles

    history = wandering(strategy.warmup_candles + 150)
    points = line_points(lines, [c.ts for c in history], [float(c.close) for c in history], HOUR)
    for i, point in enumerate(points):
        signal = strategy.on_candle(history[: i + 1], None)
        if point is None:
            assert signal.reason == "warming up"
        else:
            assert point == {key: signal.indicators[key] for key in lines.names}
