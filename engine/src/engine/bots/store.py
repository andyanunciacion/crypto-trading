"""Bots and their strategy configs in the database (`bots`, `strategy_configs`).

A bot runs the strategy config its `active_strategy_config_id` points at. Configs are
append-only: changing a bot's strategy or params adds a row (P6), so every order's
`reason` can name the exact config behind it.
"""

from dataclasses import dataclass
from typing import Any, cast

import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.brokers.base import Mode
from engine.exchange.models import Timeframe
from engine.marketdata.store import Series
from engine.risk.sizing import ALL_IN, SIZING, Sizing
from engine.risk.stops import NO_STOPS, STOPS, Stops
from engine.strategies.base import Strategy
from engine.strategies.registry import build
from shared.db.models import BotRow, OrderRow, PositionRow, StrategyConfigRow

log = structlog.get_logger()


@dataclass(frozen=True)
class Bot:
    """A running bot, with its strategy built from the active config."""

    id: int
    name: str
    exchange: str
    symbol: str
    timeframe: Timeframe
    strategy_config_id: int
    strategy: Strategy[Any]
    sizing: Sizing = ALL_IN  # how much each buy spends (D39)
    stops: Stops = NO_STOPS  # when a position is sold whatever the strategy says (D40)

    @property
    def series(self) -> Series:
        return Series(self.exchange, self.symbol, self.timeframe)


async def create_bot(
    db: AsyncEngine,
    *,
    name: str,
    exchange: str,
    symbol: str,
    timeframe: Timeframe,
    strategy: Strategy[Any],
    created_by: str,
    sizing: Sizing = ALL_IN,
    stops: Stops = NO_STOPS,
) -> int:
    """Add a running paper bot with `strategy` as its first config. Returns the bot's id.

    One transaction: a bot never exists without its config.
    """
    async with db.begin() as conn:
        bot_id: int = (
            await conn.execute(
                insert(BotRow)
                .values(
                    name=name,
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    mode="paper",
                    status="running",
                    sizing=SIZING.dump_python(sizing, mode="json"),
                    stops=STOPS.dump_python(stops, mode="json"),
                )
                .returning(BotRow.id)
            )
        ).scalar_one()
        config_id = await conn.scalar(
            insert(StrategyConfigRow)
            .values(
                bot_id=bot_id,
                strategy=strategy.name,
                version=strategy.version,
                params=strategy.params.model_dump(mode="json"),
                created_by=created_by,
            )
            .returning(StrategyConfigRow.id)
        )
        await conn.execute(
            update(BotRow).where(BotRow.id == bot_id).values(active_strategy_config_id=config_id)
        )
    return bot_id


async def load_bots(db: AsyncEngine, exchange: str) -> list[Bot]:
    """The bots with status "running" or "paused" that this engine can run, oldest first.

    A paused bot (D42) still runs: its sells go through and its stops are watched, but
    the RiskManager blocks its buys until it's set running again. A bot it can't run is
    logged (`bot.refused`, with the reason) and left out, so one bad row doesn't stop
    the others.
    """
    query = (
        select(
            BotRow.id,
            BotRow.name,
            BotRow.exchange,
            BotRow.symbol,
            BotRow.timeframe,
            BotRow.mode,
            BotRow.status,
            BotRow.sizing,
            BotRow.stops,
            StrategyConfigRow.id.label("config_id"),
            StrategyConfigRow.bot_id.label("config_bot_id"),
            StrategyConfigRow.strategy,
            StrategyConfigRow.version,
            StrategyConfigRow.params,
        )
        .select_from(BotRow)
        .outerjoin(StrategyConfigRow, StrategyConfigRow.id == BotRow.active_strategy_config_id)
        .where(BotRow.status.in_(("running", "paused")))
        .order_by(BotRow.id)
    )
    async with db.connect() as conn:
        rows = (await conn.execute(query)).all()
    bots: list[Bot] = []
    statuses: dict[int, str] = {}
    for row in rows:
        try:
            bots.append(_bot(row, exchange))
        except ValueError as exc:  # includes pydantic's ValidationError for bad params
            log.error("bot.refused", bot_id=row.id, name=row.name, reason=str(exc))
        statuses[row.id] = row.status
    log.info(
        "bots.loaded",
        bots=[
            {
                "id": b.id,
                "name": b.name,
                "status": statuses[b.id],
                "symbol": b.symbol,
                "timeframe": b.timeframe,
                "strategy": b.strategy.name,
                "params": b.strategy.params.model_dump(mode="json"),
                "stops": STOPS.dump_python(b.stops, mode="json", exclude_none=True),
            }
            for b in bots
        ],
    )
    return bots


async def held_symbols(db: AsyncEngine, *, mode: Mode) -> list[str]:
    """The symbols of every open position in `mode`, stopped bots' included.

    The engine streams their prices: the account's equity values each at its bid, and
    a coin with no fresh price blocks every buy (the stale data breaker, D42).
    """
    query = (
        select(PositionRow.symbol)
        .where(PositionRow.mode == mode, PositionRow.closed_at.is_(None))
        .distinct()
        .order_by(PositionRow.symbol)
    )
    async with db.connect() as conn:
        return list((await conn.scalars(query)).all())


async def open_orders(db: AsyncEngine, bot_id: int | None = None, *, mode: Mode) -> list[str]:
    """The client order IDs of the bot's open orders in `mode`, oldest first.

    With no `bot_id`, every bot's, stopped ones included: what a restart cancels (D38).
    """
    query = (
        select(OrderRow.client_order_id)
        .where(OrderRow.mode == mode, OrderRow.status == "open")
        .order_by(OrderRow.id)
    )
    if bot_id is not None:
        query = query.where(OrderRow.bot_id == bot_id)
    async with db.connect() as conn:
        return list((await conn.scalars(query)).all())


def _bot(row: Any, exchange: str) -> Bot:
    if row.mode != "paper":
        # D17: live trading needs LiveBroker (P9), a config flag and a confirmation.
        raise ValueError(f"{row.mode} bots can't run yet: only paper bots until P9")
    if row.exchange != exchange:
        raise ValueError(f"the engine trades on {exchange}, not {row.exchange}")
    if row.config_id is None:
        raise ValueError("no active strategy config")
    if row.config_bot_id != row.id:
        raise ValueError(
            f"its active strategy config {row.config_id} belongs to bot {row.config_bot_id}"
        )
    return Bot(
        id=row.id,
        name=row.name,
        exchange=row.exchange,
        symbol=row.symbol,
        timeframe=cast(Timeframe, row.timeframe),  # the table's check constraint allows no other
        strategy_config_id=row.config_id,
        strategy=build(row.strategy, row.version, row.params),
        sizing=SIZING.validate_python(row.sizing),
        stops=STOPS.validate_python(row.stops),
    )
