"""An account's totals, worked out from its cash, open positions and closed trades.

Pure, all Decimal, in the quote currency (USDT). The RiskManager's limits check a buy
against these (engine/risk/account.py, D41) and the dashboard shows them (D45), so the
P&L on screen is the one the daily loss limit sees.

- equity: the cash (free and held by orders), plus each open position at the bid.
- deposits: cash plus positions at cost always equals what was deposited plus every
  realized P&L (the paper ledger counts each fee when paid), so they're that minus the
  P&L.
- peak: adding the closed trades' P&L to the deposits, in the order they closed, gives
  the equity after each one; the peak is the highest of those, the deposits included.
  An open trade's loss still shows, in equity.
- day_start: the deposits plus the P&L of trades closed before midnight in Manila. An
  open position's whole move since its entry counts as today's, also when it opened
  before midnight: usually the stricter reading.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Manila is UTC+8 all year: the Philippines has had no daylight saving since 1978.
MANILA = timezone(timedelta(hours=8), "Asia/Manila")


@dataclass(frozen=True)
class Held:
    """An open position."""

    amount: Decimal
    avg_entry: Decimal
    realized_pnl: Decimal  # so far: its fees, and any part already sold
    bid: Decimal  # the latest bid (the caller's choice when there's never been one)


@dataclass(frozen=True)
class ClosedTrade:
    realized_pnl: Decimal
    closed_at: datetime


@dataclass(frozen=True)
class Totals:
    equity: Decimal
    deposits: Decimal
    peak: Decimal
    day_start: Decimal


def account_totals(
    cash: Decimal, held: Sequence[Held], closed: Sequence[ClosedTrade], midnight: datetime
) -> Totals:
    """The totals (module docstring). `midnight` is today's in Manila (`manila_midnight`)."""
    at_cost = sum((p.amount * p.avg_entry for p in held), Decimal(0))
    realized = sum((p.realized_pnl for p in held), Decimal(0)) + sum(
        (c.realized_pnl for c in closed), Decimal(0)
    )
    deposits = cash + at_cost - realized
    peak = running = deposits
    # sorted() is stable: trades closed at the same time keep the caller's order.
    for trade in sorted(closed, key=lambda c: c.closed_at):
        running += trade.realized_pnl
        peak = max(peak, running)
    day_start = deposits + sum(
        (c.realized_pnl for c in closed if c.closed_at < midnight), Decimal(0)
    )
    return Totals(
        equity=cash + sum((p.amount * p.bid for p in held), Decimal(0)),
        deposits=deposits,
        peak=peak,
        day_start=day_start,
    )


def manila_midnight(now: datetime) -> datetime:
    """When `now`'s day began in Manila: when the daily loss starts again."""
    return now.astimezone(MANILA).replace(hour=0, minute=0, second=0, microsecond=0)
