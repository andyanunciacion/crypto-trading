"""The metrics for judging a backtest (D30), and a CLI that shows them for a stored run.

They're computed from a run's config and result alone, so every stored run can be
reported on, and a corrected formula applies to old runs too. Run inside the stack:
    docker compose run --rm engine python -m engine.backtest.report 7
    docker compose run --rm -T engine python -m engine.backtest.report 7 --equity-csv
The second prints the equity curve as CSV (ts, equity, drawdown), to chart in a
spreadsheet; `-T` keeps Docker from adding `\\r` to every line.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, timedelta
from decimal import Decimal
from itertools import pairwise

from engine.backtest.models import (
    BacktestConfig,
    BacktestReport,
    BacktestResult,
    EquityPoint,
    Fill,
    Trade,
)
from engine.backtest.store import BacktestRunStore, StoredRun
from engine.exchange.models import Timeframe
from engine.main import configure_logging
from engine.marketdata.timeframes import STEP
from engine.risk.sizing import SIZING
from shared.config import Settings, load_settings
from shared.db.session import make_engine

# Crypto trades every day of the year, so a year is 365 days of candles, not the 252
# trading days used for stocks.
YEAR = timedelta(days=365)


def trades(fills: Sequence[Fill]) -> list[Trade]:
    """Pair each buy with the sell after it. A buy still open at the end isn't a trade yet."""
    closed: list[Trade] = []
    entry: Fill | None = None
    for fill in fills:
        if fill.side == "buy":
            if entry is not None:
                raise ValueError(f"buy at {fill.ts} while the buy at {entry.ts} is still open")
            entry = fill
            continue
        if entry is None:
            raise ValueError(f"sell at {fill.ts} with no buy before it")
        if fill.amount != entry.amount:
            raise ValueError(f"sell of {fill.amount} at {fill.ts} doesn't close {entry.amount}")
        cost = entry.price * entry.amount + entry.fee
        pnl = fill.price * fill.amount - fill.fee - cost
        closed.append(
            Trade(
                entry_ts=entry.ts,
                exit_ts=fill.ts,
                amount=entry.amount,
                entry_price=entry.price,
                exit_price=fill.price,
                fees=entry.fee + fill.fee,
                pnl=pnl,
                return_=pnl / cost,
            )
        )
        entry = None
    return closed


def drawdowns(initial_cash: Decimal, curve: Sequence[EquityPoint]) -> list[Decimal]:
    """Each point's fall from the highest equity so far, as a fraction of that high.

    The high starts at the starting cash: losing from the first candle is a drawdown too.
    """
    peak = initial_cash
    falls: list[Decimal] = []
    for point in curve:
        peak = max(peak, point.equity)
        falls.append((peak - point.equity) / peak)
    return falls


def sharpe(
    initial_cash: Decimal, curve: Sequence[EquityPoint], timeframe: Timeframe
) -> Decimal | None:
    """Annualized Sharpe ratio of the per-candle returns, with a risk-free rate of 0.

    The mean return per unit of its volatility (sample standard deviation), scaled by
    the square root of the candles in a year. None with fewer than two returns or no
    variation at all.
    """
    equities = [initial_cash, *(point.equity for point in curve)]
    returns = [b / a - 1 for a, b in pairwise(equities)]
    if len(returns) < 2:
        return None
    mean = sum(returns, Decimal(0)) / len(returns)
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (len(returns) - 1)
    if variance == 0:
        return None
    per_year = Decimal(YEAR // STEP[timeframe])
    return mean / variance.sqrt() * per_year.sqrt()


def report(config: BacktestConfig, result: BacktestResult) -> BacktestReport:
    closed = trades(result.fills)
    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl < 0]  # break-even trades are neither
    return BacktestReport(
        total_return=result.final_equity / config.initial_cash - 1,
        max_drawdown=max(drawdowns(config.initial_cash, result.equity_curve), default=Decimal(0)),
        sharpe=sharpe(config.initial_cash, result.equity_curve, config.timeframe),
        trades=closed,
        trade_count=len(closed),
        win_rate=Decimal(len(wins)) / len(closed) if closed else None,
        profit_factor=sum(wins, Decimal(0)) / -sum(losses, Decimal(0)) if losses else None,
        open_trade=result.final_amount > 0,
        final_equity=result.final_equity,
        total_fees=result.total_fees,
    )


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
    return "\n".join(
        [
            f"run {run_id}: {config.strategy} v{config.strategy_version}"
            f" {json.dumps(config.params)} on {config.exchange} {config.symbol} {config.timeframe},"
            f" {config.start:%Y-%m-%d %H:%M} to {config.end:%Y-%m-%d %H:%M} UTC",
            f"  fee {config.fee_rate * 100:f}%, slippage {config.slippage_rate * 100:f}%,"
            f" sizing {SIZING.dump_json(config.sizing).decode()}",
            f"  {config.initial_cash:f} {quote} -> {r.final_equity:.2f} {quote}"
            f" ({r.total_return * 100:+.2f}%), fees {r.total_fees:.2f} {quote}",
            f"  max drawdown {r.max_drawdown * 100:.2f}%, Sharpe {sharpe_text} (annualized)",
            f"  {trades_text}",
            f"  {len(result.fills)} fills, {len(result.skipped)} skipped orders"
            + (", still holding at the end" if r.open_trade else ""),
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
