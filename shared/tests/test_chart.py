"""The Market chart's indicator lines: each point over that candle's own warm-up window.

engine/tests/test_chart_parity.py checks the same lines against the strategies' signals.
"""

from datetime import UTC, datetime, timedelta

import pytest

from shared.chart import UnknownStrategy, chart_lines, line_points
from shared.indicators import bollinger, ema, rsi

START = datetime(2026, 10, 26, tzinfo=UTC)
HOUR = timedelta(hours=1)
CLOSES = [100.0, 102.0, 101.0, 105.0, 107.0, 104.0, 108.0, 111.0, 109.0, 113.0, 112.0, 115.0]


def hours(n: int) -> list[datetime]:
    return [START + i * HOUR for i in range(n)]


def test_ema_crossover_draws_both_emas_on_the_price_pane() -> None:
    lines = chart_lines("ema_crossover", {"fast": 2, "slow": 3})
    assert lines.warmup == 10  # 3 x slow + 1, as the strategy
    assert lines.pane == "price"
    assert lines.names == ("fast_ema", "slow_ema")
    assert lines.guides == {}


def test_a_point_is_computed_over_its_own_window_only() -> None:
    lines = chart_lines("ema_crossover", {"fast": 2, "slow": 3})
    points = line_points(lines, hours(len(CLOSES)), CLOSES, HOUR)
    assert points[:9] == [None] * 9  # warming up, as the strategy would be
    for i in range(9, len(CLOSES)):
        window = CLOSES[i - 9 : i + 1]
        assert points[i] == {"fast_ema": ema(window, 2)[-1], "slow_ema": ema(window, 3)[-1]}
    # Not an EMA over the whole series: the first close is outside the last window.
    changed = [50.0, *CLOSES[1:]]
    assert line_points(lines, hours(len(CLOSES)), changed, HOUR)[-1] == points[-1]
    assert line_points(lines, hours(len(CLOSES)), changed, HOUR)[9] != points[9]


def test_bollinger_breakout_draws_its_three_bands() -> None:
    lines = chart_lines("bollinger_breakout", {"period": 3, "width": 2.0, "volume_factor": 1.5})
    assert lines.warmup == 4  # period + 1
    assert lines.pane == "price"
    points = line_points(lines, hours(len(CLOSES)), CLOSES, HOUR)
    assert points[:3] == [None] * 3
    bands = bollinger(CLOSES[2:6], 3, 2.0)[-1]
    assert points[5] == {"upper": bands.upper, "middle": bands.middle, "lower": bands.lower}


def test_rsi_mean_reversion_draws_rsi_in_its_own_pane_with_its_levels() -> None:
    lines = chart_lines("rsi_mean_reversion", {"period": 2, "oversold": 25.0, "overbought": 75.0})
    assert lines.warmup == 13  # 6 x period + 1
    assert lines.pane == "oscillator"
    assert lines.names == ("rsi",)
    assert lines.guides == {"oversold": 25.0, "overbought": 75.0}
    closes = [*CLOSES, 110.0]
    points = line_points(lines, hours(len(closes)), closes, HOUR)
    assert points[:12] == [None] * 12
    assert points[12] == {"rsi": rsi(closes, 2)[-1]}


def test_a_window_with_a_gap_has_no_point_as_the_strategy_holds() -> None:
    lines = chart_lines("bollinger_breakout", {"period": 2, "width": 2.0, "volume_factor": 0})
    ts = hours(len(CLOSES))
    ts[6:] = [t + HOUR for t in ts[6:]]  # the candle after ts[5] is missing
    points = line_points(lines, ts, CLOSES, HOUR)
    assert points[5] is not None
    assert points[6:8] == [None, None]  # their 3-candle windows span the gap
    assert points[8] is not None


def test_points_can_start_part_way() -> None:
    lines = chart_lines("ema_crossover", {"fast": 2, "slow": 3})
    every = line_points(lines, hours(len(CLOSES)), CLOSES, HOUR)
    assert line_points(lines, hours(len(CLOSES)), CLOSES, HOUR, first=10) == every[10:]


def test_an_unknown_strategy_has_no_lines() -> None:
    with pytest.raises(UnknownStrategy, match="martingale"):
        chart_lines("martingale", {})


def test_params_the_lines_cant_read_are_refused() -> None:
    with pytest.raises(ValueError, match="fast"):
        chart_lines("ema_crossover", {"slow": 21})
    with pytest.raises(ValueError, match="period"):
        chart_lines("rsi_mean_reversion", {"period": "x", "oversold": 30, "overbought": 70})
