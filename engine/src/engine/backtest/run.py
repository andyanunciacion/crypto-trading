"""Backtest a strategy on stored candles, and store the run.

Run inside the stack (Postgres has no host port), after backfilling the candles:
    docker compose run --rm engine python -m engine.backtest.run ema_crossover BTC/USDT 1h
Add `--params '{"fast": 9, "slow": 21}'` to change the strategy's settings. The range
ends at the last stored candle and goes back `--days`; the strategy's warm-up candles
come from before that.
"""

import argparse
import asyncio
import sys
from typing import Any

import structlog

from engine.backtest.cli import add_run_arguments, json_object_arg, run_settings
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
from engine.main import configure_logging
from engine.marketdata.store import CandleStore, Series
from engine.marketdata.timeframes import STEP
from engine.strategies.base import Strategy
from engine.strategies.registry import STRATEGIES, build, strategy_class
from shared.config import Settings, load_settings
from shared.db.session import make_engine

log = structlog.get_logger()


async def load_candles(candles: CandleStore, config: BacktestConfig, warmup: int) -> list[Candle]:
    """The range's candles, plus `warmup` candles before it."""
    step = STEP[config.timeframe]
    series = Series(config.exchange, config.symbol, config.timeframe)
    return await candles.candles(series, config.start - warmup * step, config.end)


async def backtest(
    candles: CandleStore, runs: BacktestRunStore, strategy: Strategy[Any], config: BacktestConfig
) -> tuple[int, BacktestResult]:
    """Run `strategy` as `config` says and store the run. Returns the run's id and result."""
    warmup = warmup_needed(strategy, config)
    history = await load_candles(candles, config, warmup)
    result = run_backtest(strategy, history, config)
    run_id = await runs.save(config, warmup, history, result)
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
    )
    return run_id, result


async def reproduce(candles: CandleStore, runs: BacktestRunStore, run_id: int) -> BacktestResult:
    """Run a stored backtest again from its stored inputs.

    Refuses if anything it depends on has changed since: the backtester, the strategy's
    version or the candles. Otherwise the result must equal the stored one.
    """
    stored = await runs.load(run_id)
    config = stored.config
    if stored.backtester_version != BACKTESTER_VERSION:
        raise BacktestError(
            f"run {run_id} used backtester version {stored.backtester_version}, "
            f"this is version {BACKTESTER_VERSION}"
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
        run_id, result = await backtest(candles, BacktestRunStore(db), strategy, config)
        print(describe(run_id, config, result))
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
