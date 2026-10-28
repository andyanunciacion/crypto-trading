"""The Bots page's list: every bot with how it's set up (D47), read fresh each time.

Its live status also comes once a second with the Overview (dashboard/overview.py);
this adds what the Overview leaves out: the strategy's params, sizing, stops and cash.
The engine writes all of it; the API only reads.
"""

from decimal import Decimal
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from shared.commands import SwitchPolicy
from shared.db.models import BotRow, PaperBalanceRow, StrategyConfigRow


class BotSetup(BaseModel):
    # Ignores any column it doesn't show.
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int
    name: str
    created_at: AwareDatetime
    exchange: str
    symbol: str
    timeframe: str
    mode: str
    status: str
    switch_policy: SwitchPolicy  # what a strategy switch does with its position (D51)
    active_strategy_config_id: int | None
    strategy: str | None  # None: no active strategy config (the engine refuses it)
    strategy_version: int | None
    params: dict[str, Any] | None
    sizing: dict[str, Any]
    stops: dict[str, Any]
    currency: str  # the quote currency
    cash: Decimal  # free + held by open orders, in the quote currency
    paused_reason: str | None
    paused_at: AwareDatetime | None
    stopped_reason: str | None
    stopped_at: AwareDatetime | None


async def read_bots(db: AsyncEngine) -> list[BotSetup]:
    """Every bot, oldest first."""
    query = (
        select(
            *BotRow.__table__.columns,
            StrategyConfigRow.strategy,
            StrategyConfigRow.version.label("strategy_version"),
            StrategyConfigRow.params,
        )
        .outerjoin(StrategyConfigRow, StrategyConfigRow.id == BotRow.active_strategy_config_id)
        .order_by(BotRow.id)
    )
    balances_query = select(
        PaperBalanceRow.bot_id, PaperBalanceRow.currency, PaperBalanceRow.free, PaperBalanceRow.used
    )
    async with db.connect() as conn:
        rows = (await conn.execute(query)).all()
        balances = (await conn.execute(balances_query)).all()
    cash: dict[tuple[int, str], Decimal] = {
        (b.bot_id, b.currency): b.free + b.used for b in balances
    }
    bots: list[BotSetup] = []
    for row in rows:
        currency = row.symbol.split("/")[-1]
        bots.append(
            BotSetup.model_validate(
                {
                    **row._mapping,
                    "currency": currency,
                    "cash": cash.get((row.id, currency), Decimal(0)),
                }
            )
        )
    return bots
