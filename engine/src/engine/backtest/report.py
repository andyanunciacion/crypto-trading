"""The metrics for judging a backtest (D30), and a CLI that shows them for a stored run.

They're computed from a run's config and result alone, so every stored run can be
reported on, and a corrected formula applies to old runs too. The formulas live in
shared/backtest.py, which the dashboard uses too, so both show the same numbers (D48).
Run inside the stack:
    docker compose run --rm engine python -m engine.backtest.report 7
    docker compose run --rm -T engine python -m engine.backtest.report 7 --equity-csv
The second prints the equity curve as CSV (ts, equity, drawdown), to chart in a
spreadsheet; `-T` keeps Docker from adding `\\r` to every line.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC
from decimal import Decimal

from engine.backtest.models import BacktestConfig, BacktestReport, BacktestResult, EquityPoint
from engine.backtest.store import BacktestRunStore, StoredRun
from engine.risk.sizing import SIZING
from engine.risk.stops import NO_STOPS, STOPS
from shared.backtest import drawdowns, metrics, sharpe, trades
from shared.config import Settings, load_settings
from shared.db.session import make_engine
from shared.logs import configure_logging

__all__ = ["describe", "drawdowns", "equity_csv", "report", "sharpe", "trades"]


def report(config: BacktestConfig, result: BacktestResult) -> BacktestReport:
    return metrics(config.initial_cash, config.timeframe, result)


def describe(run_id: int, config: BacktestConfig, result: BacktestResult) -> str:
    r = report(config, result)
    quote = config.symbol.split("/")[1]
    sharpe_text = "n/a" if r.sharpe is None else f"{r.sharpe:.2f}"
    if r.win_rate is None:
        trades_text = "no closed trades"
    else:
        noun = "trade" if r.trade_count == 1 else "trades"
        factor = "n/a (no losing trades)" if r.profit_factor is None else f"{r.profit_factor:.2f}"
        trades_text = f"{r.trade_count} {noun}, {r.win_rate * 100:.1f}% won, profit factor {factor}"
    stops_text = (
        ""
        if config.stops == NO_STOPS
        else f", stops {STOPS.dump_json(config.stops, exclude_none=True).decode()}"
    )
    # How many sells each kind of stop made, in the order they first happened.
    exits = Counter(f.exit for f in result.fills if f.exit is not None)
    exits_text = [f"  exits: {', '.join(f'{n} {kind}' for kind, n in exits.items())}"]
    return "\n".join(
        [
            f"run {run_id}: {config.strategy} v{config.strategy_version}"
            f" {json.dumps(config.params)} on {config.exchange} {config.symbol} {config.timeframe},"
            f" {config.start:%Y-%m-%d %H:%M} to {config.end:%Y-%m-%d %H:%M} UTC",
            f"  fee {config.fee_rate * 100:f}%, slippage {config.slippage_rate * 100:f}%,"
            f" sizing {SIZING.dump_json(config.sizing).decode()}{stops_text}",
            f"  {config.initial_cash:f} {quote} -> {r.final_equity:.2f} {quote}"
            f" ({r.total_return * 100:+.2f}%), fees {r.total_fees:.2f} {quote}",
            f"  max drawdown {r.max_drawdown * 100:.2f}%, Sharpe {sharpe_text} (annualized)",
            f"  {trades_text}",
            f"  {len(result.fills)} fills, {len(result.skipped)} skipped orders"
            + (", still holding at the end" if r.open_trade else ""),
            *(exits_text if exits else []),
        ]
    )


def equity_csv(initial_cash: Decimal, curve: Sequence[EquityPoint]) -> str:
    """One row per candle: its open time (UTC), equity at its close, and drawdown."""
    rows = ["ts,equity,drawdown"]
    for point, fall in zip(curve, drawdowns(initial_cash, curve), strict=True):
        rows.append(f"{point.ts.astimezone(UTC).isoformat()},{point.equity:f},{fall:.6f}")
    return "\n".join(rows) + "\n"


async def load(run_id: int, settings: Settings) -> StoredRun:
    db = make_engine(settings.database_url)
    try:
        return await BacktestRunStore(db).load(run_id)
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_id", type=int)
    parser.add_argument(
        "--equity-csv", action="store_true", help="print the equity curve as CSV instead"
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level, sys.stderr)  # stdout carries the report
    try:
        stored = asyncio.run(load(args.run_id, settings))
    except (LookupError, ValueError) as e:
        print(f"report failed: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    if args.equity_csv:
        print(equity_csv(stored.config.initial_cash, stored.result.equity_curve), end="")
    else:
        print(describe(stored.id, stored.config, stored.result))


if __name__ == "__main__":
    main()
