"""The daily summary: sent at 08:00 Manila, about the Manila day before (D59).

The day is midnight to midnight in Manila, the day the daily loss limit counts (D41).
"Now" is the Overview the dashboard shows (D45), so equity, P&L and drawdown are worked
out by the formulas the limits use. Yesterday's equity change isn't stored anywhere
(D41 keeps no snapshots), so for yesterday the summary counts what is: trades closed
and their realized P&L, orders filled, buys blocked, strategy switches.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.dashboard.overview import Overview, read_overview
from shared.account import manila_midnight
from shared.db.models import FillRow, OrderRow, PositionRow, RiskEventRow, StrategySwitchRow


@dataclass(frozen=True)
class DaySummary:
    day: date  # the Manila day it's about
    overview: Overview  # now
    trades_closed: int
    realized_pnl: Decimal  # of those trades, net of fees
    orders_filled: int  # orders with a fill that day
    blocked: dict[str, int]  # buys blocked that day, by rule
    switches: int


async def read_summary(
    db: AsyncEngine, redis: Redis, *, mode: Literal["paper", "live"], now: datetime
) -> DaySummary:
    end = manila_midnight(now)
    start = end - timedelta(days=1)
    async with db.connect() as conn:
        closed = (
            await conn.execute(
                select(func.count(), func.coalesce(func.sum(PositionRow.realized_pnl), 0)).where(
                    PositionRow.mode == "paper",
                    PositionRow.closed_at >= start,
                    PositionRow.closed_at < end,
                )
            )
        ).one()
        filled = await conn.scalar(
            select(func.count(distinct(FillRow.order_id)))
            .join(OrderRow, OrderRow.id == FillRow.order_id)
            .where(OrderRow.mode == "paper", FillRow.ts >= start, FillRow.ts < end)
        )
        blocked = (
            await conn.execute(
                select(RiskEventRow.rule, func.count())
                .where(
                    RiskEventRow.mode == "paper",
                    RiskEventRow.decision == "blocked",
                    RiskEventRow.ts >= start,
                    RiskEventRow.ts < end,
                )
                .group_by(RiskEventRow.rule)
                .order_by(func.count().desc(), RiskEventRow.rule)
            )
        ).all()
        switches = await conn.scalar(
            select(func.count()).where(StrategySwitchRow.ts >= start, StrategySwitchRow.ts < end)
        )
    return DaySummary(
        day=start.date(),
        overview=await read_overview(db, redis, mode=mode, now=now),
        trades_closed=closed[0],
        realized_pnl=Decimal(closed[1]),
        orders_filled=filled or 0,
        blocked={rule: count for rule, count in blocked},
        switches=switches or 0,
    )
