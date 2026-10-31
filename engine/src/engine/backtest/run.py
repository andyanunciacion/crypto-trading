"""Backtest a strategy on stored candles, and store the run.

Run inside the stack (Postgres has no host port), after backfilling the candles:
    docker compose run --rm engine python -m engine.backtest.run ema_crossover BTC/USDT 1h
Add `--params '{"fast": 9, "slow": 21}'` to change the strategy's settings. The range
ends at the last stored candle and goes back `--days`; the strategy's warm-up candles
come from before that. `--auto-switch '{"mapping": {...}}'` switches strategy as the
regime changes, as an auto-switching bot does (P6-5), starting on the strategy given,
and also runs each of its configs alone over the same candles (compare.py), storing
them with the run and printing them beside it (D57).
"""

import argparse
import asyncio
import sys
from typing import Any

import structlog

from engine.backtest.cli import (
    AUTO_SWITCH_HELP,
    add_run_arguments,
    auto_switch_arg,
    json_object_arg,
    run_settings,
)
from engine.backtest.compare import Baseline, baselines, describe_baselines
from engine.backtest.models import BacktestConfig, BacktestResult
from engine.backtest.report import describe, report
from engine.backtest.simulator import (
    BACKTESTER_VERSION,
    BacktestError,
    run_backtest,
    warmup_needed,
)
from engine.backtest.store import BacktestRunStore, candles_sha256
from engine.exchange.models import Candle
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP
from engine.regime.detector import RegimeDetector
from engine.strategies.base import Strategy
from engine.strategies.registry import STRATEGIES, build, strategy_class
from shared.config import Settings, load_settings
from shared.db.session import make_engine
from shared.logs import configure_logging

log = structlog.get_logger()


async def load_candles(candles: CandleStore, config: BacktestConfig, warmup: int) -> list[Candle]:
    """The range's candles, plus `warmup` candles before it."""
    step = STEP[config.timeframe]
    series = Series(config.exchange, config.symbol, config.timeframe)
    return await candles.candles(series, config.start - warmup * step, config.end)


def _run_and_compare(
    strategy: Strategy[Any], history: list[Candle], config: BacktestConfig
) -> tuple[BacktestResult, list[Baseline] | None]:
    result = run_backtest(strategy, history, config)
    found = None if config.auto_switch is None else baselines(strategy, history, config)
    return result, found


async def backtest(
    candles: CandleStore, runs: BacktestRunStore, strategy: Strategy[Any], config: BacktestConfig
) -> tuple[int, BacktestResult, list[Baseline] | None]:
    """Run `strategy` as `config` says and store the run. Returns the run's id, its result
    and, for an auto-switching run, its baselines (compare.py), stored with it (D57)."""
    warmup = warmup_needed(strategy, config)
    history = await load_candles(candles, config, warmup)
    # CPU-bound: in a thread, so the backtester's heartbeat keeps beating (worker.py).
    result, found = await asyncio.to_thread(_run_and_compare, strategy, history, config)
    run_id = await runs.save(config, warmup, history, result, found)
    metrics = report(config, result)
    log.info(
        "backtest.done",
        run_id=run_id,
        **config.model_dump(mode="json"),
        candles=len(history),
        fills=len(result.fills),
        skipped=len(result.skipped),
        final_equity=str(result.final_equity),
        total_fees=str(result.total_fees),
        total_return=str(metrics.total_return),
        max_drawdown=str(metrics.max_drawdown),
        sharpe=None if metrics.sharpe is None else str(metrics.sharpe),
        trades=metrics.trade_count,
        win_rate=None if metrics.win_rate is None else str(metrics.win_rate),
        profit_factor=None if metrics.profit_factor is None else str(metrics.profit_factor),
        baselines=None if found is None else len(found),
    )
    return run_id, result, found


async def reproduce(candles: CandleStore, runs: BacktestRunStore, run_id: int) -> BacktestResult:
    """Run a stored backtest again from its stored inputs.

    Refuses if anything it depends on has changed since: the backtester, the strategy's
    version (or a mapped one's), the regime detector's or the candles. Otherwise the
    result must equal the stored one.
    """
    stored = await runs.load(run_id)
    config = stored.config
    if stored.backtester_version != BACKTESTER_VERSION:
        raise BacktestError(
            f"run {run_id} used backtester version {stored.backtester_version}, "
            f"this is version {BACKTESTER_VERSION}"
        )
    detector = stored.regime_detector_version
    if config.auto_switch is not None and detector != RegimeDetector.version:
        raise BacktestError(
            f"run {run_id} used regime detector version {detector}, "
            f"this is version {RegimeDetector.version}"
        )
    strategy = build(config.strategy, config.strategy_version, config.params)
    history = await load_candles(candles, config, stored.warmup_candles)
    if candles_sha256(history) != stored.candles_sha256:
        raise BacktestError(
            f"the stored candles for run {run_id} have changed since it ran "
            f"({len(history)} now, {stored.candle_count} then)"
        )
    return run_backtest(strategy, history, config)


async def run(args: argparse.Namespace, settings: Settings) -> None:
    strategy = build(args.strategy, strategy_class(args.strategy).version, args.params)
    db = make_engine(settings.database_url)
    candles = CandleStore(db)
    try:
        config = await run_settings(args, candles, strategy)
        run_id, result, found = await backtest(candles, BacktestRunStore(db), strategy, config)
        print(describe(run_id, config, result))
        if found is not None:
            print(describe_baselines(config, result, found))
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("strategy", choices=sorted(STRATEGIES))
    add_run_arguments(parser)
    parser.add_argument(
        "--params",
        type=json_object_arg,
        default={},
        help="strategy params as JSON (default: its own)",
    )
    parser.add_argument("--auto-switch", type=auto_switch_arg, help=AUTO_SWITCH_HELP)
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(args, settings))
    except (BacktestError, ValueError) as e:  # ValueError includes pydantic's ValidationError
        print(f"backtest failed: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
