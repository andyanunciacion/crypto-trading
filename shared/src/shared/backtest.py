"""A backtest's fills and equity curve, and the metrics for judging it (D30, D48).

Pure, all Decimal. The engine's backtester produces these (engine/backtest/), and the
metrics are worked out from them each time they're shown, never stored: by the CLI's
report and by the dashboard's Backtests page alike, with this one function, so both show
the same numbers for the same run (D48).

`StoredResult` reads a stored run's `result` (`backtest_runs.result`) without the
engine: it's the engine's `BacktestResult` with the signals left as JSON.
"""

import json
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from shared.candles import TIMEFRAME_STEPS
from shared.regime import MappedRegime

# The engine's OrderSide and ExitKind (a stop's sell); engine/tests checks they match.
Side = Literal["buy", "sell"]
ExitKind = Literal["stop_loss", "trailing_stop", "take_profit"]
# What sold a position other than a SELL signal: a stop, or an auto-switch with the
# policy `close_first` (P6-5; a live bot's exits of kind `switch`, D51).
FillExit = ExitKind | Literal["switch"]

# Crypto trades every day of the year, so a year is 365 days of candles, not the 252
# trading days used for stocks.
YEAR = timedelta(days=365)


class _Frozen(BaseModel):
    # Immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ConfigRef(_Frozen):
    """A strategy config: a strategy at a version, with its params."""

    strategy: str
    version: int
    params: dict[str, JsonValue]

    @property
    def label(self) -> str:
        # As the switcher's reasons name a config (engine/regime/switcher.py).
        return f"{self.strategy} v{self.version} {json.dumps(self.params)}"


class Fill(_Frozen):
    ts: AwareDatetime  # open time of the candle it filled at
    side: Side
    price: Decimal = Field(gt=0)  # after slippage
    amount: Decimal = Field(gt=0)  # in the base currency
    fee: Decimal = Field(ge=0)  # in the quote currency
    cash_after: Decimal = Field(ge=0)
    # The stop or switch that sold it; None for a fill from a signal. Runs stored before
    # stops existed have no such field and load as None.
    exit: FillExit | None = None
    # In an auto-switching run, the config whose signal it was; None in any other run,
    # and for an exit.
    decided_by: ConfigRef | None = None


class BacktestSwitch(_Frozen):
    """A switch an auto-switching run made (P6-5): confirmed at the close of the candle
    at `ts`, so `to_config` decides from the next candle on."""

    ts: AwareDatetime
    regime: MappedRegime
    from_config: ConfigRef
    to_config: ConfigRef
    reason: str  # the switcher's, as a live bot logs it


class SkippedOrder(_Frozen):
    """A BUY or SELL signal that didn't become a fill, and why."""

    signal_ts: AwareDatetime
    side: Side
    reason: str


class EquityPoint(_Frozen):
    ts: AwareDatetime  # the candle's open time; equity is valued at its close
    equity: Decimal


class StoredResult(_Frozen):
    """A stored run's result, as `backtest_runs.result` holds it."""

    signals: list[dict[str, JsonValue]]  # the engine's Signals, as JSON
    fills: list[Fill]
    skipped: list[SkippedOrder]
    equity_curve: list[EquityPoint]  # one point per candle in the range
    final_cash: Decimal
    final_amount: Decimal  # coin still held at the end; 0 when flat
    final_equity: Decimal  # cash + coin at the last close
    total_fees: Decimal
    switches: list[BacktestSwitch] = []  # auto-switching runs only (P6-5)


class BaselineResult(_Frozen):
    """One of the baselines an auto-switching run is judged against (P6-5c, D57): a config
    it switches between, run alone over the same candles, or buy-and-hold. Stored with the
    run (`backtest_runs.baselines`) with only what the metrics need, which are worked out
    when shown, like the run's own (D30)."""

    label: str
    config: ConfigRef | None  # None for buy-and-hold, which no run can switch to
    fills: list[Fill]
    equity_curve: list[EquityPoint]
    final_amount: Decimal
    final_equity: Decimal
    total_fees: Decimal


class Outcome(Protocol):
    """What the metrics need from a run's result: a `StoredResult` or the engine's own."""

    @property
    def fills(self) -> Sequence[Fill]: ...
    @property
    def equity_curve(self) -> Sequence[EquityPoint]: ...
    @property
    def final_amount(self) -> Decimal: ...
    @property
    def final_equity(self) -> Decimal: ...
    @property
    def total_fees(self) -> Decimal: ...


class Trade(_Frozen):
    """A round trip: a buy fill and the sell fill that closed it."""

    entry_ts: AwareDatetime
    exit_ts: AwareDatetime
    amount: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    exit_price: Decimal = Field(gt=0)
    fees: Decimal = Field(ge=0)  # both fills' fees
    pnl: Decimal  # net of both fees, in the quote currency
    return_: Decimal  # pnl as a fraction of what the buy cost, fee included
    exit: FillExit | None = None  # the stop or switch that sold it; None for a SELL signal
    decided_by: ConfigRef | None = None  # the buy's config, in an auto-switching run


class BacktestReport(_Frozen):
    """The metrics for judging a run (D30). Derived from a result, never stored.

    Ratios are fractions (0.05 = 5%). A metric that can't be computed is None.
    """

    total_return: Decimal
    max_drawdown: Decimal  # largest fall from a peak, as a fraction of that peak
    sharpe: Decimal | None  # annualized, risk-free rate 0
    trades: list[Trade]  # closed round trips only
    trade_count: int
    win_rate: Decimal | None  # None with no trades
    profit_factor: Decimal | None  # gross profit / gross loss; None with no losing trades
    open_trade: bool  # still holding at the end: in total_return, not in the trade stats
    final_equity: Decimal
    total_fees: Decimal


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
                exit=fill.exit,
                decided_by=entry.decided_by,
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


def sharpe(initial_cash: Decimal, curve: Sequence[EquityPoint], timeframe: str) -> Decimal | None:
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
    per_year = Decimal(YEAR // TIMEFRAME_STEPS[timeframe])
    return mean / variance.sqrt() * per_year.sqrt()


def metrics(initial_cash: Decimal, timeframe: str, result: Outcome) -> BacktestReport:
    """Every metric of a run that started with `initial_cash` on `timeframe` candles."""
    closed = trades(result.fills)
    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl < 0]  # break-even trades are neither
    return BacktestReport(
        total_return=result.final_equity / initial_cash - 1,
        max_drawdown=max(drawdowns(initial_cash, result.equity_curve), default=Decimal(0)),
        sharpe=sharpe(initial_cash, result.equity_curve, timeframe),
        trades=closed,
        trade_count=len(closed),
        win_rate=Decimal(len(wins)) / len(closed) if closed else None,
        profit_factor=sum(wins, Decimal(0)) / -sum(losses, Decimal(0)) if losses else None,
        open_trade=result.final_amount > 0,
        final_equity=result.final_equity,
        total_fees=result.total_fees,
    )
