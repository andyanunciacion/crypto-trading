"""Grid sweeps: backtest every combination of a strategy's params and rank them (P2-5).

A sweep alone finds the params that fitted the past best, which is exactly how a
strategy gets overfitted: walk_forward.py tests the winner on candles it never saw.
Run inside the stack, after backfilling:
    docker compose run --rm engine python -m engine.backtest.sweep ema_crossover BTC/USDT 1h \\
        --grid '{"fast": [5, 9, 12], "slow": [21, 30, 50]}' --days 360
Only the winner is stored in `backtest_runs`.
"""

import argparse
import asyncio
import itertools
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import JsonValue, ValidationError

from engine.backtest.cli import add_run_arguments, json_object_arg, run_settings
from engine.backtest.models import BacktestConfig, BacktestReport, BacktestResult
from engine.backtest.report import report
from engine.backtest.run import load_candles
from engine.backtest.simulator import (
    BacktestError,
    check_contiguous,
    run_backtest,
    warmup_needed,
)
from engine.backtest.store import BacktestRunStore
from engine.exchange.models import Candle, Timeframe
from engine.main import configure_logging
from engine.marketdata.store import CandleStore
from engine.marketdata.timeframes import STEP
from engine.strategies.base import Strategy
from engine.strategies.registry import STRATEGIES, strategy_class
from shared.config import Settings, load_settings
from shared.db.session import make_engine

log = structlog.get_logger()


@dataclass(frozen=True)
class InvalidParams:
    """A combination the strategy's params model refuses, like fast >= slow."""

    params: dict[str, JsonValue]
    reason: str


@dataclass(frozen=True)
class Grid:
    strategies: list[Strategy[Any]]  # one per valid combination, in grid order
    invalid: list[InvalidParams]


def param_grid(cls: type[Strategy[Any]], grid: Mapping[str, Sequence[JsonValue]]) -> Grid:
    """Every combination of the listed values; params left out keep their defaults.

    The first param varies slowest: {"fast": [3, 5], "slow": [8, 13]} gives (3, 8),
    (3, 13), (5, 8), (5, 13). Combinations the params model refuses are kept aside with
    the reason, so a grid can cross two params without listing only valid pairs.
    """
    fields = cls.params_model.model_fields
    for name, values in grid.items():
        if name not in fields:
            raise ValueError(f"unknown param {name!r} for {cls.name}; it has {', '.join(fields)}")
        if not isinstance(values, list):
            raise ValueError(f"give a list of values for {name!r}, like [{values!r}]")
        if not values:
            raise ValueError(f"no values for {name!r}")
    strategies: list[Strategy[Any]] = []
    invalid: list[InvalidParams] = []
    for combination in itertools.product(*grid.values()):
        params = dict(zip(grid, combination, strict=True))
        try:
            strategies.append(cls(cls.params_model.model_validate(params)))
        except ValidationError as e:
            reason = "; ".join(
                str(error["msg"]).removeprefix("Value error, ") for error in e.errors()
            )
            invalid.append(InvalidParams(params=params, reason=reason))
    return Grid(strategies, invalid)


def configure(
    base: BacktestConfig, strategy: Strategy[Any], start: datetime, end: datetime
) -> BacktestConfig:
    """`base`'s run settings (market, fees, cash) for `strategy` over [start, end)."""
    return BacktestConfig(
        **(
            dict(base)
            | {
                "strategy": strategy.name,
                "strategy_version": strategy.version,
                "params": strategy.params.model_dump(mode="json"),
                "start": start,
                "end": end,
            }
        )
    )


def candles_for(
    candles: Sequence[Candle], start: datetime, end: datetime, warmup: int, timeframe: Timeframe
) -> Sequence[Candle]:
    """The candles one run needs: exactly `warmup` before `start`, then up to `end`.

    Exactly, so a stored run's candles hash the same when `reproduce()` loads them.
    `candles` must be contiguous (check_contiguous).
    """
    step = STEP[timeframe]
    first = (start - candles[0].ts) // step
    stop = (end - candles[0].ts) // step
    if first < warmup:
        raise BacktestError(
            f"needs {warmup} warm-up candles before {start:%Y-%m-%d %H:%M} UTC, has {max(first, 0)}"
        )
    if stop > len(candles):
        raise BacktestError(
            f"candles end at {candles[-1].ts:%Y-%m-%d %H:%M} UTC, before {end:%Y-%m-%d %H:%M} UTC"
        )
    return candles[first - warmup : stop]


@dataclass(frozen=True)
class SweepEntry:
    """One param set's backtest, and everything needed to store it."""

    strategy: Strategy[Any]
    config: BacktestConfig
    candles: Sequence[Candle]  # what it ran on, warm-up included
    result: BacktestResult
    report: BacktestReport


def _by_sharpe(entry: SweepEntry) -> tuple[bool, Decimal]:
    # Best first; no Sharpe (no trades, or no variation) last.
    sharpe = entry.report.sharpe
    return (sharpe is None, Decimal(0) if sharpe is None else -sharpe)


def sweep(
    strategies: Sequence[Strategy[Any]], candles: Sequence[Candle], base: BacktestConfig
) -> list[SweepEntry]:
    """Backtest each strategy over `base`'s range, best Sharpe first.

    Every run trades the same candles, whatever its warm-up, so they compare fairly;
    `candles` must start early enough for the longest warm-up. Ties keep their order.
    """
    if not strategies:
        raise ValueError("no valid param sets to sweep")
    check_contiguous(candles, base.timeframe)
    entries: list[SweepEntry] = []
    for strategy in strategies:
        config = configure(base, strategy, base.start, base.end)
        warmup = warmup_needed(strategy, config)
        own = candles_for(candles, base.start, base.end, warmup, base.timeframe)
        result = run_backtest(strategy, own, config)
        entries.append(SweepEntry(strategy, config, own, result, report(config, result)))
    return sorted(entries, key=_by_sharpe)  # sorted() is stable: ties keep grid order


# --- Output -----------------------------------------------------------------------------


def params_text(params: Mapping[str, JsonValue]) -> str:
    return " ".join(f"{name}={value}" for name, value in params.items())


METRIC_HEADERS = ("return", "max DD", "Sharpe", "trades")
METRIC_WIDTHS = (8, 7, 7, 6)


OPEN_NOTE = "  * still holding at the end: counted in the return, not in the trades"


def metric_cells(r: BacktestReport) -> list[str]:
    """Return, max drawdown, Sharpe and closed trades (* if still holding), for a table."""
    return [
        f"{r.total_return * 100:+.2f}%",
        f"{r.max_drawdown * 100:.2f}%",
        "n/a" if r.sharpe is None else f"{r.sharpe:.2f}",
        f"{r.trade_count}{'*' if r.open_trade else ''}",
    ]


def column_widths(rows: Sequence[Sequence[str]]) -> list[int]:
    return [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]


def table(rows: Sequence[Sequence[str]], left: int) -> list[str]:
    """Align columns, two spaces apart: the first `left` to the left, numbers to the right."""
    widths = column_widths(rows)
    return [
        "  "
        + "  ".join(
            cell.ljust(w) if i < left else cell.rjust(w)
            for i, (cell, w) in enumerate(zip(row, widths, strict=True))
        ).rstrip()
        for row in rows
    ]


def describe(entries: Sequence[SweepEntry], invalid: Sequence[InvalidParams], run_id: int) -> str:
    config = entries[0].config
    rows = [["rank", "params", *METRIC_HEADERS, "won"]]
    for rank, entry in enumerate(entries, 1):
        r = entry.report
        won = "n/a" if r.win_rate is None else f"{r.win_rate * 100:.1f}%"
        rows.append([str(rank), params_text(entry.config.params), *metric_cells(r), won])
    skipped = f" ({len(invalid)} skipped)" if invalid else ""
    return "\n".join(
        [
            f"sweep: {config.strategy} v{config.strategy_version} on {config.exchange}"
            f" {config.symbol} {config.timeframe}, {config.start:%Y-%m-%d %H:%M} to"
            f" {config.end:%Y-%m-%d %H:%M} UTC",
            f"  {len(entries)} param set{'' if len(entries) == 1 else 's'}{skipped},"
            " ranked by Sharpe;"
            f" fee {config.fee_rate * 100:f}%, slippage {config.slippage_rate * 100:f}%",
            *table(rows, left=2),
            *([OPEN_NOTE] if any(e.report.open_trade for e in entries) else []),
            *(f"  skipped {params_text(i.params)}: {i.reason}" for i in invalid),
            f"  best stored as run {run_id}",
        ]
    )


# --- CLI --------------------------------------------------------------------------------


def max_warmup(strategies: Sequence[Strategy[Any]], base: BacktestConfig) -> int:
    return max(warmup_needed(s, base) for s in strategies)


async def save(runs: BacktestRunStore, entry: SweepEntry) -> int:
    warmup = warmup_needed(entry.strategy, entry.config)
    return await runs.save(entry.config, warmup, entry.candles, entry.result)


async def run(args: argparse.Namespace, settings: Settings) -> None:
    grid = param_grid(strategy_class(args.strategy), args.grid)
    if not grid.strategies:
        raise BacktestError("every combination in the grid is invalid")
    db = make_engine(settings.database_url)
    try:
        candles = CandleStore(db)
        base = await run_settings(args, candles, grid.strategies[0])
        history = await load_candles(candles, base, max_warmup(grid.strategies, base))
        entries = sweep(grid.strategies, history, base)
        run_id = await save(BacktestRunStore(db), entries[0])
        best = entries[0].report
        log.info(
            "sweep.done",
            **base.model_dump(mode="json", exclude={"strategy_version", "params"}),
            param_sets=len(entries),
            skipped=len(grid.invalid),
            best_params=entries[0].config.params,
            best_sharpe=None if best.sharpe is None else str(best.sharpe),
            run_id=run_id,
        )
        print(describe(entries, grid.invalid, run_id))
    finally:
        await db.dispose()


def add_grid_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--grid",
        type=json_object_arg,
        default={},
        help='values to try per param, as JSON, e.g. \'{"fast": [5, 9], "slow": [21, 30]}\'',
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("strategy", choices=sorted(STRATEGIES))
    add_run_arguments(parser)
    add_grid_argument(parser)
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(args, settings))
    except (BacktestError, ValueError) as e:  # ValueError includes pydantic's ValidationError
        print(f"sweep failed: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
