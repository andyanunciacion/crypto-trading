"""The Bots page's list: every bot with how it's set up (D47), read fresh each time; and
a bot's detail page: the same, with every strategy config it has had (P6-2, D52) and
every switch between them (P6-4, D54).

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
from shared.db.models import BotRow, PaperBalanceRow, StrategyConfigRow, StrategySwitchRow


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
    # Its auto-switching (shared.regime.AutoSwitchSettings, as stored; P6-4); None is off.
    auto_switch: dict[str, Any] | None


class ConfigEntry(BaseModel):
    """One of a bot's strategy configs (P6-2, D52): each is when that config became its."""

    model_config = ConfigDict(frozen=True)

    id: int
    number: int  # the bot's own count: its first config is 1
    strategy: str
    version: int
    params: dict[str, Any]
    created_at: AwareDatetime
    created_by: str
    restored_from: int | None  # a rollback's: the config (id) it copies
    active: bool  # the one the bot runs now


class SwitchEntry(BaseModel):
    """One switch of a bot's config (P6-4, D54): by hand, a rollback, or the regime."""

    model_config = ConfigDict(frozen=True)

    id: int
    ts: AwareDatetime
    from_config: int | None  # the configs' ids; `*_number` are the bot's own counts
    from_number: int | None
    to_config: int
    to_number: int
    trigger: str  # manual, rollback or regime
    policy: SwitchPolicy | None  # None for those recorded from the config history
    regime: str | None
    candle_ts: AwareDatetime | None  # the candle that confirmed the regime
    reading: dict[str, Any] | None  # the detector's reading there, and how long it held
    reason: str
    source: str


class BotDetail(BaseModel):
    """The bot detail page: the bot, every config it has had and every switch, newest
    first."""

    model_config = ConfigDict(frozen=True)

    bot: BotSetup
    configs: list[ConfigEntry]
    switches: list[SwitchEntry]


async def read_bot(db: AsyncEngine, bot_id: int) -> BotDetail | None:
    """Bot `bot_id`, its config history and its switches; None if there's no such bot."""
    found = await read_bots(db, bot_id)
    if not found:
        return None
    bot = found[0]
    query = (
        select(StrategyConfigRow)
        .where(StrategyConfigRow.bot_id == bot_id)
        .order_by(StrategyConfigRow.id)
    )
    switches_query = (
        select(StrategySwitchRow)
        .where(StrategySwitchRow.bot_id == bot_id)
        .order_by(StrategySwitchRow.id.desc())
    )
    async with db.connect() as conn:
        rows = (await conn.execute(query)).all()
        switch_rows = (await conn.execute(switches_query)).all()
    numbers = {row.id: number for number, row in enumerate(rows, start=1)}
    switches = [
        SwitchEntry(
            id=row.id,
            ts=row.ts,
            from_config=row.from_config,
            from_number=None if row.from_config is None else numbers.get(row.from_config),
            to_config=row.to_config,
            to_number=numbers[row.to_config],
            trigger=row.trigger,
            policy=row.policy,
            regime=row.regime,
            candle_ts=row.candle_ts,
            reading=row.reading,
            reason=row.reason,
            source=row.source,
        )
        for row in switch_rows
    ]
    configs = [
        ConfigEntry(
            id=row.id,
            number=number,
            strategy=row.strategy,
            version=row.version,
            params=row.params,
            created_at=row.created_at,
            created_by=row.created_by,
            restored_from=row.restored_from,
            active=row.id == bot.active_strategy_config_id,
        )
        for number, row in enumerate(rows, start=1)
    ]
    return BotDetail(bot=bot, configs=configs[::-1], switches=switches)


async def read_bots(db: AsyncEngine, bot_id: int | None = None) -> list[BotSetup]:
    """Every bot, oldest first; or only bot `bot_id` (none if there's no such bot)."""
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
    if bot_id is not None:
        query = query.where(BotRow.id == bot_id)
        balances_query = balances_query.where(PaperBalanceRow.bot_id == bot_id)
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
