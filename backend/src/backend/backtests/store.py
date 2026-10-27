"""The Backtests page's reads (D48): stored runs with their metrics, and what candles
are stored for a series.

The metrics are worked out each time a run is shown, never stored (D30), with
shared.backtest.metrics: the function the CLI's report uses, so the page shows the
CLI's numbers. The backtester writes the runs; the API only reads.
"""

import asyncio
from decimal import Decimal
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from shared.backtest import (
    BacktestReport,
    SkippedOrder,
    StoredResult,
    Trade,
    drawdowns,
    metrics,
)
from shared.db.models import BacktestRunRow, CandleRow

# A drawdown is shown to 6 places (0.0001%), as the CLI's equity CSV writes it.
DRAWDOWN_PLACES = Decimal("0.000001")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Metrics(_Frozen):
    """A run's report (shared.backtest.BacktestReport) without its trades. Ratios are
    fractions (0.05 = 5%); None where there's nothing to compute one from."""

    total_return: Decimal
    max_drawdown: Decimal
    sharpe: Decimal | None  # annualized, risk-free rate 0
    trade_count: int  # closed round trips
    win_rate: Decimal | None
    profit_factor: Decimal | None
    open_trade: bool  # still holding at the end
    final_equity: Decimal
    total_fees: Decimal


class RunInputs(_Frozen):
    """What a run was run with, as backtest_runs stores it."""

    id: int
    created_at: AwareDatetime
    strategy: str
    strategy_version: int
    params: dict[str, Any]
    exchange: str
    symbol: str
    timeframe: str
    currency: str  # the quote currency, which the cash and P&L are in
    range_start: AwareDatetime  # the first candle traded
    range_end: AwareDatetime  # exclusive
    warmup_candles: int
    initial_cash: Decimal
    fee_rate: Decimal
    slippage_rate: Decimal
    sizing: dict[str, Any]
    stops: dict[str, Any]
    candle_count: int
    candles_sha256: str
    backtester_version: int


class RunSummary(RunInputs):
    metrics: Metrics | None  # None if the stored result can't be read (see `problem`)
    problem: str | None = None


class EquityPoint(_Frozen):
    ts: AwareDatetime  # the candle's open time; equity at its close
    equity: Decimal
    drawdown: Decimal  # the fall from the highest equity so far, as a fraction of it


class RunDetail(RunSummary):
    trades: list[Trade]  # closed round trips, oldest first
    equity: list[EquityPoint]  # one point per candle in the range
    skipped: list[SkippedOrder]  # signals that didn't become fills, and why
    fills: int
    signals: int


class CandleRange(_Frozen):
    exchange: str
    symbol: str
    timeframe: str
    first: AwareDatetime | None  # None: nothing stored
    last: AwareDatetime | None
    count: int


def _inputs(row: RowMapping) -> dict[str, Any]:
    fields = RunInputs.model_fields.keys() - {"currency"}
    return {name: row[name] for name in fields} | {"currency": row["symbol"].split("/")[-1]}


def _report(row: RowMapping) -> tuple[StoredResult, BacktestReport] | str:
    """The stored result and its report, or why there's none."""
    try:
        result = StoredResult.model_validate(row["result"])
        return result, metrics(row["initial_cash"], row["timeframe"], result)
    except (ValidationError, ValueError) as exc:
        # A run stored by an older backtester that no longer reads: shown, without metrics.
        return f"its result can't be read: {exc}"


def _shown(report: BacktestReport) -> Metrics:
    return Metrics.model_validate(report.model_dump(exclude={"trades"}))


def _summary(row: RowMapping) -> RunSummary:
    match _report(row):
        case str(problem):
            return RunSummary(**_inputs(row), metrics=None, problem=problem)
        case _, report:
            return RunSummary(**_inputs(row), metrics=_shown(report))


def _detail(row: RowMapping) -> RunDetail:
    match _report(row):
        case str(problem):
            return RunDetail(
                **_inputs(row),
                metrics=None,
                problem=problem,
                trades=[],
                equity=[],
                skipped=[],
                fills=0,
                signals=0,
            )
        case result, report:
            falls = drawdowns(row["initial_cash"], result.equity_curve)
            return RunDetail(
                **_inputs(row),
                metrics=_shown(report),
                trades=report.trades,
                equity=[
                    EquityPoint(ts=p.ts, equity=p.equity, drawdown=fall.quantize(DRAWDOWN_PLACES))
                    for p, fall in zip(result.equity_curve, falls, strict=True)
                ],
                skipped=result.skipped,
                fills=len(result.fills),
                signals=len(result.signals),
            )


async def read_runs(conn: AsyncConnection, *, limit: int, before: int | None) -> list[RunSummary]:
    """The newest `limit` runs (older than run `before`), newest first, with their metrics."""
    query = select(BacktestRunRow.__table__).order_by(BacktestRunRow.id.desc()).limit(limit)
    if before is not None:
        query = query.where(BacktestRunRow.id < before)
    rows = (await conn.execute(query)).mappings().all()
    # Decimal math over every equity point: in a thread, so the event loop isn't held up.
    return await asyncio.to_thread(lambda: [_summary(row) for row in rows])


async def read_run(conn: AsyncConnection, run_id: int) -> RunDetail | None:
    query = select(BacktestRunRow.__table__).where(BacktestRunRow.id == run_id)
    row = (await conn.execute(query)).mappings().one_or_none()
    return None if row is None else await asyncio.to_thread(_detail, row)


async def read_candle_range(
    conn: AsyncConnection, exchange: str, symbol: str, timeframe: str
) -> CandleRange:
    query = select(func.min(CandleRow.ts), func.max(CandleRow.ts), func.count()).where(
        CandleRow.exchange == exchange, CandleRow.symbol == symbol, CandleRow.timeframe == timeframe
    )
    first, last, count = (await conn.execute(query)).one()
    return CandleRange(
        exchange=exchange, symbol=symbol, timeframe=timeframe, first=first, last=last, count=count
    )
