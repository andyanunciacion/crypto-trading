"""What an auto-switching run is judged against (P6-5, D55): its configs on their own.

Switching only pays if the combined run beats each config it switches between run
alone over the same candles, net of what switching costs (the switcher's lag, and the
round trips a `close_first` sale makes). So beside the combined run the CLI runs each
distinct config with the same costs, sizing and stops, and buy-and-hold (all-in, no
stops). They're computed when the run is, printed, and not stored, like a sweep's
losers (D32).

Pure and synchronous, like the simulator.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from engine.backtest.models import BacktestConfig, BacktestResult
from engine.backtest.simulator import BacktestError, config_ref, run_backtest
from engine.exchange.models import Candle
from engine.regime.switcher import AutoSwitcher, same_config
from engine.risk.sizing import ALL_IN
from engine.risk.stops import NO_STOPS
from engine.strategies.base import Position, Signal, Strategy, StrategyParams
from shared.backtest import BacktestReport, metrics


@dataclass(frozen=True)
class Baseline:
    label: str
    result: BacktestResult


class BuyAndHoldParams(StrategyParams):
    pass


class BuyAndHold(Strategy[BuyAndHoldParams]):
    """Buys once, at the first open of the range, and holds to the end. Not registered:
    a yardstick for backtests, never a bot's strategy."""

    name = "buy_and_hold"
    version = 1
    params_model = BuyAndHoldParams

    @property
    def warmup_candles(self) -> int:
        return 1

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        ts = window[-1].ts
        if position is None:
            return Signal(action="buy", ts=ts, reason="buy and hold")
        return Signal(action="hold", ts=ts, reason="buy and hold")


def baselines(
    strategy: Strategy[Any], candles: Sequence[Candle], config: BacktestConfig
) -> list[Baseline]:
    """Each distinct config of an auto-switching run (the one it starts on, then each
    regime's, in mapping order) run on its own over the same `candles`, then buy-and-hold."""
    if config.auto_switch is None:
        raise BacktestError("baselines are for an auto-switching run")
    configs: list[Strategy[Any]] = []
    for candidate in [strategy, *AutoSwitcher(config.auto_switch).targets.values()]:
        if not any(same_config(candidate, seen) for seen in configs):
            configs.append(candidate)
    found = [
        Baseline(config_ref(s).label, run_backtest(s, candles, _alone(config, s))) for s in configs
    ]
    hold = BuyAndHold(BuyAndHoldParams())
    all_in = _alone(config, hold).model_copy(update={"sizing": ALL_IN, "stops": NO_STOPS})
    found.append(Baseline("buy and hold", run_backtest(hold, candles, all_in)))
    return found


def _alone(config: BacktestConfig, strategy: Strategy[Any]) -> BacktestConfig:
    return config.model_copy(
        update={
            "strategy": strategy.name,
            "strategy_version": strategy.version,
            "params": strategy.params.model_dump(mode="json"),
            "auto_switch": None,
        }
    )


def describe_baselines(
    config: BacktestConfig, combined: BacktestResult, found: Sequence[Baseline]
) -> str:
    """The combined run and its baselines side by side, best total return first."""
    rows = [("auto-switching (this run)", combined), *((b.label, b.result) for b in found)]
    reports = [(label, metrics(config.initial_cash, config.timeframe, r)) for label, r in rows]
    reports.sort(key=lambda row: row[1].total_return, reverse=True)
    width = max(len(label) for label, _ in reports)
    lines = ["  against each config alone, over the same candles (not stored):"]
    for label, r in reports:
        lines.append(f"    {label:<{width}}  {_numbers(r)}")
    return "\n".join(lines)


def _numbers(r: BacktestReport) -> str:
    sharpe = "  n/a" if r.sharpe is None else f"{r.sharpe:5.2f}"
    noun = "trade" if r.trade_count == 1 else "trades"
    return (
        f"{r.total_return * 100:+8.2f}%  max drawdown {r.max_drawdown * 100:6.2f}%"
        f"  Sharpe {sharpe}  {r.trade_count} {noun}" + (", open at the end" if r.open_trade else "")
    )
