"""Walk-forward testing: the overfitting check for a parameter sweep (P2-5, D32).

The best params on past candles ("in-sample") always look good: they were picked for
it. Walk-forward picks them on a train window, then tests them unchanged on the window
after it ("out-of-sample"), which the choice never saw, and rolls forward. If the
out-of-sample numbers fall far below the in-sample ones, the params fitted noise.
Run inside the stack, after backfilling:
    docker compose run --rm engine python -m engine.backtest.walk_forward ema_crossover \\
        BTC/USDT 1h --grid '{"fast": [5, 9, 12], "slow": [21, 30, 50]}' --days 360 \\
        --train-days 90 --test-days 30
Each window's in-sample winner and its out-of-sample run are stored in `backtest_runs`.
"""

import argparse
import asyncio
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from engine.backtest.cli import add_run_arguments, run_settings
from engine.backtest.models import BacktestConfig, BacktestReport
from engine.backtest.run import load_candles
from engine.backtest.simulator import BacktestError, check_contiguous
from engine.backtest.store import BacktestRunStore
from engine.backtest.sweep import (
    METRIC_HEADERS,
    OPEN_NOTE,
    Grid,
    SweepEntry,
    add_grid_argument,
    column_widths,
    max_warmup,
    metric_cells,
    param_grid,
    params_text,
    save,
    sweep,
    table,
)
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
class Window:
    """Pick params on [train_start, test_start), then test them on [test_start, test_end)."""

    train_start: datetime
    test_start: datetime
    test_end: datetime


def windows(
    start: datetime, end: datetime, train: timedelta, test: timedelta, timeframe: Timeframe
) -> list[Window]:
    """Rolling windows over [start, end): the train window keeps its length and moves on
    by `test` each time, so the test windows follow each other without overlapping.

    The last test window ends at `end`, so the most recent candles are always tested; a
    leftover too short for another window is dropped from the start.
    """
    step = STEP[timeframe]
    for name, length in (("train", train), ("test", test)):
        if length <= timedelta(0) or length % step:
            raise ValueError(f"the {name} window must be a whole number of {timeframe} candles")
    count = (end - start - train) // test
    if count < 1:
        raise BacktestError(
            f"one window needs at least {length_text(train + test)} of candles;"
            f" the range has {length_text(end - start)}"
        )
    starts = [end - (count - k) * test for k in range(count)]
    return [Window(s - train, s, s + test) for s in starts]


@dataclass(frozen=True)
class WindowResult:
    window: Window
    in_sample: SweepEntry  # the best param set on the train window
    out_of_sample: SweepEntry  # the same params on the test window


def _over(base: BacktestConfig, start: datetime, end: datetime) -> BacktestConfig:
    return BacktestConfig(**(dict(base) | {"start": start, "end": end}))


def walk_forward(
    strategies: Sequence[Strategy[Any]],
    candles: Sequence[Candle],
    base: BacktestConfig,
    wins: Sequence[Window],
) -> list[WindowResult]:
    """Sweep `strategies` on each train window and test the winner on its test window.

    `base` gives the market and costs; its range is ignored. Every test run starts flat
    with `base.initial_cash`. Its warm-up candles come from the end of the train window:
    that's history a live bot would have too, not a look at the future.
    """
    check_contiguous(candles, base.timeframe)  # a gap in a late window fails before any work
    results: list[WindowResult] = []
    for n, w in enumerate(wins, 1):
        best = sweep(strategies, candles, _over(base, w.train_start, w.test_start))[0]
        tested = sweep([best.strategy], candles, _over(base, w.test_start, w.test_end))[0]
        results.append(WindowResult(w, best, tested))
        log.info(
            "walk_forward.window",
            window=n,
            train_start=w.train_start.isoformat(),
            test_start=w.test_start.isoformat(),
            test_end=w.test_end.isoformat(),
            params=best.config.params,
            in_sample=_metrics(best.report),
            out_of_sample=_metrics(tested.report),
        )
    return results


def _metrics(r: BacktestReport) -> dict[str, str | int | None]:
    return {
        "total_return": str(r.total_return),
        "max_drawdown": str(r.max_drawdown),
        "sharpe": None if r.sharpe is None else str(r.sharpe),
        "trades": r.trade_count,
    }


def compounded(returns: Iterable[Decimal]) -> Decimal:
    """The return of reinvesting through every window: (1 + r1)(1 + r2)... - 1."""
    total = Decimal(1)
    for r in returns:
        total *= 1 + r
    return total - 1


def mean_sharpe(reports: Sequence[BacktestReport]) -> Decimal | None:
    """The average Sharpe of the runs that have one; None if none does."""
    sharpes = [r.sharpe for r in reports if r.sharpe is not None]
    return sum(sharpes, Decimal(0)) / len(sharpes) if sharpes else None


# --- Output -----------------------------------------------------------------------------


def length_text(span: timedelta) -> str:
    for unit, name in ((timedelta(days=1), "day"), (timedelta(hours=1), "hour")):
        if not span % unit:
            n = span // unit
            return f"{n} {name}{'' if n == 1 else 's'}"
    return f"{span // timedelta(minutes=1)} minutes"


def describe(
    results: Sequence[WindowResult], grid: Grid, run_ids: Sequence[tuple[int, int]]
) -> str:
    config = results[0].in_sample.config
    wins = [r.window for r in results]
    first, last = wins[0], wins[-1]
    # Whole-day windows all start at the same time of day: show it once, dates in rows.
    times = {ts.timetz() for w in wins for ts in (w.train_start, w.test_start)}
    fmt = "%Y-%m-%d" if len(times) == 1 else "%Y-%m-%d %H:%M"
    at_time = f", every window starting at {first.test_start:%H:%M} UTC" if len(times) == 1 else ""

    rows = [["#", "train from", "test from", "params", *METRIC_HEADERS, *METRIC_HEADERS, "runs"]]
    for n, (r, (in_id, out_id)) in enumerate(zip(results, run_ids, strict=True), 1):
        rows.append(
            [
                str(n),
                f"{r.window.train_start:{fmt}}",
                f"{r.window.test_start:{fmt}}",
                params_text(r.in_sample.config.params),
                *metric_cells(r.in_sample.report),
                *metric_cells(r.out_of_sample.report),
                f"{in_id}, {out_id}",
            ]
        )
    lines = table(rows, left=4)

    # A label over each group of metric columns, so the two sides read as pairs.
    widths = column_widths(rows)
    metrics = len(METRIC_HEADERS)

    def span(first_col: int) -> tuple[int, int]:
        offset = 2 + sum(widths[:first_col]) + 2 * first_col
        return offset, sum(widths[first_col : first_col + metrics]) + 2 * (metrics - 1)

    in_at, in_width = span(4)
    out_at, out_width = span(4 + metrics)
    groups = (
        " " * in_at
        + " in-sample (train) ".center(in_width, "-")
        + " " * (out_at - in_at - in_width)
        + " out-of-sample (test) ".center(out_width, "-")
    )

    in_reports = [r.in_sample.report for r in results]
    out_reports = [r.out_of_sample.report for r in results]
    quote = config.symbol.split("/")[1]
    skipped = f" ({len(grid.invalid)} skipped)" if grid.invalid else ""
    tested = sum((w.test_end - w.test_start for w in wins), timedelta(0))

    def sharpe_text(value: Decimal | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    return "\n".join(
        [
            f"walk-forward: {config.strategy} v{config.strategy_version} on {config.exchange}"
            f" {config.symbol} {config.timeframe}, {first.train_start:%Y-%m-%d %H:%M} to"
            f" {last.test_end:%Y-%m-%d %H:%M} UTC",
            f"  rolling, {len(results)} windows: best of {len(grid.strategies)} param sets{skipped}"
            f" by Sharpe on {length_text(first.test_start - first.train_start)},"
            f" then tested unchanged on the {length_text(first.test_end - first.test_start)}"
            f" after{at_time}",
            f"  fee {config.fee_rate * 100:f}%, slippage {config.slippage_rate * 100:f}%;"
            f" each test starts flat with {config.initial_cash:f} {quote}",
            "",
            groups.rstrip(),
            *lines,
            *([OPEN_NOTE] if any(r.open_trade for r in in_reports + out_reports) else []),
            "",
            f"  out-of-sample: {compounded(r.total_return for r in out_reports) * 100:+.2f}%"
            f" compounded over {length_text(tested)},"
            f" {sum(r.trade_count for r in out_reports)} closed trades",
            f"  mean Sharpe: {sharpe_text(mean_sharpe(in_reports))} in-sample,"
            f" {sharpe_text(mean_sharpe(out_reports))} out-of-sample",
            "  runs: the in-sample and out-of-sample run ids; show one with"
            " `python -m engine.backtest.report <id>`",
        ]
    )


# --- CLI --------------------------------------------------------------------------------


async def run_walk_forward(
    candles: CandleStore,
    runs: BacktestRunStore,
    strategies: Sequence[Strategy[Any]],
    base: BacktestConfig,
    wins: Sequence[Window],
) -> tuple[list[WindowResult], list[tuple[int, int]]]:
    """Walk forward on stored candles and store each window's two runs.

    Returns the results and, per window, the in-sample and out-of-sample run ids.
    """
    span = _over(base, wins[0].train_start, wins[-1].test_end)
    history = await load_candles(candles, span, max_warmup(strategies, base))
    results = walk_forward(strategies, history, base, wins)
    run_ids: list[tuple[int, int]] = []
    for r in results:
        run_ids.append((await save(runs, r.in_sample), await save(runs, r.out_of_sample)))
    return results, run_ids


async def run(args: argparse.Namespace, settings: Settings) -> None:
    grid = param_grid(strategy_class(args.strategy), args.grid)
    if not grid.strategies:
        raise BacktestError("every combination in the grid is invalid")
    db = make_engine(settings.database_url)
    try:
        candles = CandleStore(db)
        base = await run_settings(args, candles, grid.strategies[0])
        train, test = timedelta(days=args.train_days), timedelta(days=args.test_days)
        wins = windows(base.start, base.end, train, test, base.timeframe)
        results, run_ids = await run_walk_forward(
            candles, BacktestRunStore(db), grid.strategies, base, wins
        )
        out_reports = [r.out_of_sample.report for r in results]
        log.info(
            "walk_forward.done",
            strategy=base.strategy,
            symbol=base.symbol,
            timeframe=base.timeframe,
            windows=len(results),
            param_sets=len(grid.strategies),
            skipped=len(grid.invalid),
            out_of_sample_return=str(compounded(r.total_return for r in out_reports)),
        )
        print(describe(results, grid, run_ids))
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("strategy", choices=sorted(STRATEGIES))
    add_run_arguments(parser)
    add_grid_argument(parser)
    parser.add_argument(
        "--train-days", type=int, default=90, help="in-sample window, in days (default 90)"
    )
    parser.add_argument(
        "--test-days", type=int, default=30, help="out-of-sample window, in days (default 30)"
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(args, settings))
    except (BacktestError, ValueError) as e:  # ValueError includes pydantic's ValidationError
        print(f"walk-forward failed: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
