"""Grid sweeps: every combination of params, run over the same candles and ranked."""

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from engine.backtest.models import BacktestConfig
from engine.backtest.report import report
from engine.backtest.simulator import BacktestError, run_backtest
from engine.backtest.sweep import candles_for, describe, param_grid, sweep
from engine.exchange.models import Candle, MarketRules
from engine.strategies.base import Position, Signal, Strategy, StrategyParams
from engine.strategies.ema_crossover import EmaCrossover
from engine.strategies.rsi_mean_reversion import RsiMeanReversion

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
D = Decimal

RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=D("0.000001"),
    price_step=D("0.01"),
    min_amount=D("0.000001"),
    min_cost=D("1"),
)


def at(hour: int) -> datetime:
    return T0 + hour * HOUR


def wave(hour: int) -> Candle:
    # A slow sine wave with a fast wobble on top, so the EMAs cross several times.
    close = round(30000 + 2000 * math.sin(hour / 20) + 150 * math.sin(hour), 2)
    open_ = round(close * (1 + 0.001 * math.cos(hour)), 2)
    o, c = D(str(open_)), D(str(close))
    return Candle(
        ts=at(hour), open=o, high=max(o, c) + 10, low=min(o, c) - 10, close=c, volume=D(1)
    )


def waves(hours: range) -> list[Candle]:
    return [wave(h) for h in hours]


def base(start: int, end: int, **overrides: Any) -> BacktestConfig:
    """The run settings; sweep() swaps in each strategy and its params."""
    values: dict[str, Any] = {
        "strategy": "placeholder",
        "strategy_version": 1,
        "params": {},
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start": at(start),
        "end": at(end),
        "initial_cash": D(1000),
        "fee_rate": D("0.001"),
        "slippage_rate": D("0.0005"),
        "market_rules": RULES,
    }
    return BacktestConfig(**(values | overrides))


class PeriodicParams(StrategyParams):
    every: int = 0  # 0 never trades
    warmup: int = 1


class Periodic(Strategy[PeriodicParams]):
    """Buys every `every` candles and sells halfway between: trades that don't care about price."""

    name = "periodic"
    version = 1
    params_model = PeriodicParams

    @property
    def warmup_candles(self) -> int:
        return self.params.warmup

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        ts = window[-1].ts
        hour, every = (ts - T0) // HOUR, self.params.every
        if every and position is None and hour % every == 0:
            return Signal(action="buy", ts=ts, reason="its turn")
        if every and position is not None and hour % every == every // 2:
            return Signal(action="sell", ts=ts, reason="its turn")
        return Signal(action="hold", ts=ts, reason="waiting")


def params(strategies: Sequence[Strategy[Any]]) -> list[dict[str, Any]]:
    return [s.params.model_dump(mode="json") for s in strategies]


# --- The grid ---------------------------------------------------------------------------


def test_the_grid_is_every_combination_in_order() -> None:
    grid = param_grid(EmaCrossover, {"fast": [3, 5], "slow": [8, 13]})

    assert params(grid.strategies) == [
        {"fast": 3, "slow": 8},
        {"fast": 3, "slow": 13},
        {"fast": 5, "slow": 8},
        {"fast": 5, "slow": 13},
    ]
    assert grid.invalid == []
    assert all(isinstance(s, EmaCrossover) for s in grid.strategies)


def test_params_left_out_of_the_grid_keep_their_defaults() -> None:
    grid = param_grid(RsiMeanReversion, {"period": [7, 14]})

    assert params(grid.strategies) == [
        {"period": 7, "oversold": 30, "overbought": 70},
        {"period": 14, "oversold": 30, "overbought": 70},
    ]


def test_an_empty_grid_is_the_defaults_alone() -> None:
    assert params(param_grid(EmaCrossover, {}).strategies) == [{"fast": 9, "slow": 21}]


def test_invalid_combinations_are_skipped_with_their_reason() -> None:
    grid = param_grid(EmaCrossover, {"fast": [1, 5, 13], "slow": [8, 13]})

    assert params(grid.strategies) == [{"fast": 5, "slow": 8}, {"fast": 5, "slow": 13}]
    assert [i.params for i in grid.invalid] == [
        {"fast": 1, "slow": 8},
        {"fast": 1, "slow": 13},
        {"fast": 13, "slow": 8},
        {"fast": 13, "slow": 13},
    ]
    assert "greater than or equal to 2" in grid.invalid[0].reason  # fast's own limit
    assert grid.invalid[2].reason == "fast (13) must be shorter than slow (8)"


@pytest.mark.parametrize(
    ("grid", "message"),
    [
        ({"fats": [3]}, "unknown param 'fats'"),
        ({"fast": []}, "no values for 'fast'"),
        ({"fast": 3}, "list of values for 'fast'"),
    ],
    ids=["unknown", "empty", "not-a-list"],
)
def test_a_malformed_grid_is_an_error(grid: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        param_grid(EmaCrossover, grid)


# --- Candle slices ----------------------------------------------------------------------


def test_each_run_gets_exactly_its_own_warm_up_and_the_range() -> None:
    history = waves(range(100))

    sliced = candles_for(history, at(50), at(80), 20, "1h")

    assert (sliced[0].ts, sliced[-1].ts, len(sliced)) == (at(30), at(79), 50)


def test_a_slice_without_enough_warm_up_is_refused() -> None:
    with pytest.raises(BacktestError, match=r"needs 20 warm-up candles .* has 10"):
        candles_for(waves(range(100)), at(10), at(80), 20, "1h")


def test_a_slice_past_the_last_candle_is_refused() -> None:
    with pytest.raises(BacktestError, match="end"):
        candles_for(waves(range(100)), at(50), at(101), 20, "1h")


# --- The sweep --------------------------------------------------------------------------


def test_the_sweep_ranks_by_sharpe_and_puts_none_last() -> None:
    grid = param_grid(Periodic, {"every": [0, 6, 10, 24, 40]})

    entries = sweep(grid.strategies, waves(range(400)), base(10, 400))

    sharpes = [e.report.sharpe for e in entries]
    assert sharpes[-1] is None  # every=0 never traded: flat equity has no Sharpe
    known = [s for s in sharpes[:-1] if s is not None]
    assert len(known) == 4
    assert known == sorted(known, reverse=True)
    assert len({id(e.strategy) for e in entries}) == 5  # one entry per param set


def test_ties_keep_the_grid_order() -> None:
    # Neither trades, so both have no Sharpe; the first in the grid stays first.
    grid = param_grid(Periodic, {"warmup": [3, 1]})

    entries = sweep(grid.strategies, waves(range(100)), base(10, 100))

    assert [e.strategy for e in entries] == grid.strategies


def test_every_param_set_trades_the_same_range_whatever_its_warm_up() -> None:
    grid = param_grid(EmaCrossover, {"slow": [13, 40]})  # warm-ups 40 and 121
    history = waves(range(600))

    entries = sweep(grid.strategies, history, base(200, 600))

    curves = [[p.ts for p in e.result.equity_curve] for e in entries]
    assert curves[0] == curves[1]
    assert (curves[0][0], curves[0][-1], len(curves[0])) == (at(200), at(599), 400)


def test_each_entry_is_a_plain_backtest_with_its_own_config() -> None:
    # Nothing carries over from one run to the next.
    grid = param_grid(EmaCrossover, {"fast": [3, 5], "slow": [8, 13]})
    history = waves(range(300))

    entries = sweep(grid.strategies, history, base(100, 300))

    for entry in entries:
        s = entry.strategy
        assert entry.config.strategy == "ema_crossover"
        assert entry.config.params == s.params.model_dump(mode="json")
        assert (entry.config.start, entry.config.end) == (at(100), at(300))
        assert entry.candles[0].ts == at(100) - s.warmup_candles * HOUR
        alone = run_backtest(
            s, candles_for(history, at(100), at(300), s.warmup_candles, "1h"), entry.config
        )
        assert entry.result == alone
        assert entry.report == report(entry.config, alone)


def test_a_gap_is_refused_before_any_run() -> None:
    history = waves(range(300))
    del history[250]

    with pytest.raises(BacktestError, match="gap"):
        sweep(param_grid(EmaCrossover, {}).strategies, history, base(100, 300))


def test_a_sweep_needs_a_param_set() -> None:
    with pytest.raises(ValueError, match="no valid param sets"):
        sweep([], waves(range(300)), base(100, 300))


# --- Output -----------------------------------------------------------------------------


def test_describe_lists_the_ranking_and_the_skipped_combinations() -> None:
    grid = param_grid(EmaCrossover, {"fast": [3, 13], "slow": [8, 13]})
    entries = sweep(grid.strategies, waves(range(300)), base(100, 300))

    text = describe(entries, grid.invalid, run_id=42)

    lines = text.splitlines()
    assert lines[0].startswith("sweep: ema_crossover v1 on mexc BTC/USDT 1h, 2026-01-05 04:00")
    assert "2 param sets (2 skipped), ranked by Sharpe" in text
    assert "fast=3 slow=8" in text
    assert "fast=3 slow=13" in text
    assert "skipped fast=13 slow=8: " in text
    assert "best stored as run 42" in text
    header = next(line for line in lines if line.split()[0] == "rank")
    for column in ("params", "return", "max DD", "Sharpe", "trades", "won"):
        assert column in header


def test_a_run_still_holding_at_the_end_is_starred() -> None:
    # every=10 buys at hour 90 and would sell at 95, but the range ends at 94.
    entries = sweep(
        param_grid(Periodic, {"every": [10]}).strategies, waves(range(95)), base(10, 95)
    )
    assert entries[0].report.open_trade

    text = describe(entries, [], run_id=1)

    assert "1 param set, ranked" in text
    row = next(line for line in text.splitlines() if "every=10" in line)
    assert row.split()[6] == "8*"  # 8 closed round trips, and one still open
    assert "* still holding at the end" in text
