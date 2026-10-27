"""Stops on a bot's open position (D40): their levels in the database, and what a new
price does to them.

A buy records its stops in its `reason` (`"stops"`: the settings, and the ATR at its
signal's candle). Once it fills, the levels are placed from the position's average
entry and kept on the `positions` row, so a restarted engine carries on with them. If
another slice of the buy moves the average, they're placed again from the new one,
keeping the trailing stop's peak (`reentry`). The settings come from the buy, not the
bot, so an open trade keeps the stops it was opened with.

On each price (`on_bid`, pure): a bid at or below the stop, or at or above the
take-profit, sells the whole position. Once a stop has fired, the position is sold
until it's gone, whatever the price does next, as in a backtest. An exit that no
longer reaches the bid is replaced by a new one at the new bid; one that still does is
left to fill. Otherwise a new high lifts the trailing stop, never lowering it.

Each exit has its own client order ID, from the buy that opened the position: buy
`b4-202610241429-b` is sold by `b4-202610241429-x1`, then `-x2` if that one was cancelled
with something left, and so on. A retry never reuses a cancelled exit's ID.

The kill switch closes positions with the same exits (risk/kill.py, D43), as kind
`kill_switch`, so it carries on a stop's numbering, and from then on the position is
sold until it's gone by the same rule (`next_exit`). So does stopping one bot with
"Stop + close position" on the Bots page (D47), as kind `close`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.brokers.base import Mode
from engine.risk.stops import (
    STOPS,
    ExitKind,
    ExitState,
    Stops,
    after_price,
    exit_at_price,
)
from engine.strategies.base import Position
from shared.db.models import OrderRow, PositionRow

# Why an exit sells: one of the stops, the kill switch closing everything (D43), or one
# bot stopped and closed from the Bots page (D47).
ExitCause = ExitKind | Literal["kill_switch", "close"]


@dataclass(frozen=True)
class StopPlan:
    """What a buy records about its stops: the settings, and the ATR they're placed by."""

    stops: Stops
    atr: Decimal | None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "settings": STOPS.dump_python(self.stops, mode="json"),
            "atr": None if self.atr is None else str(self.atr),
        }

    @classmethod
    def from_reason(cls, reason: dict[str, Any]) -> "StopPlan | None":
        """The plan in an order's `reason`; None for a buy placed without stops."""
        value = reason.get("stops")
        if value is None:
            return None
        atr = value["atr"]
        return cls(STOPS.validate_python(value["settings"]), None if atr is None else Decimal(atr))


@dataclass(frozen=True)
class ExitOrder:
    """An order a stop, or the kill switch, placed to sell a position."""

    client_order_id: str
    status: str
    limit: Decimal
    kind: ExitCause


@dataclass(frozen=True)
class Held:
    """A bot's open position, with what its stops need."""

    position_id: int
    position: Position
    levels: ExitState | None  # as last placed; None until they are
    levels_entry: Decimal | None  # the average entry they were placed from
    buy_id: str  # the client order ID of the buy that opened it
    plan: StopPlan | None  # None: its buy had no stops
    exits: list[ExitOrder]  # oldest first

    @property
    def levels_current(self) -> bool:
        """Whether the levels were placed from the position's average entry as it is now."""
        return self.levels is not None and self.levels_entry == self.position.avg_entry


def exit_order_id(buy_id: str, attempt: int) -> str:
    """The client order ID of the `attempt`th exit of the position `buy_id` opened."""
    if not buy_id.endswith("-b"):
        raise ValueError(f"{buy_id!r} isn't a buy's client order ID")
    if attempt < 1:
        raise ValueError(f"attempt must be at least 1, not {attempt}")
    return f"{buy_id[:-1]}x{attempt}"


async def load_held(db: AsyncEngine, bot_id: int, symbol: str, *, mode: Mode) -> Held | None:
    """The bot's open position in `symbol`, its levels, its buy and its exits so far.

    None when flat, or for a position no buy of the bot opened (one made by hand).
    """
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(PositionRow.__table__).where(
                    PositionRow.bot_id == bot_id,
                    PositionRow.symbol == symbol,
                    PositionRow.mode == mode,
                    PositionRow.closed_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        # One position at a time, and a buy only when flat: the newest buy that filled
        # anything opened this one.
        buy = (
            await conn.execute(
                select(OrderRow.client_order_id, OrderRow.reason)
                .where(
                    OrderRow.bot_id == bot_id,
                    OrderRow.symbol == symbol,
                    OrderRow.mode == mode,
                    OrderRow.side == "buy",
                    OrderRow.filled > 0,
                )
                .order_by(OrderRow.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if buy is None:
            return None
        exits = (
            await conn.execute(
                select(OrderRow.client_order_id, OrderRow.status, OrderRow.price, OrderRow.reason)
                .where(
                    OrderRow.bot_id == bot_id,
                    OrderRow.client_order_id.startswith(
                        exit_order_id(buy.client_order_id, 1)[:-1], autoescape=True
                    ),
                )
                .order_by(OrderRow.id)
            )
        ).all()
    levels = (
        None
        if row.levels_entry is None
        else ExitState(
            stop=row.stop,
            take_profit=row.take_profit,
            peak=row.peak,
            stop_loss=row.stop_loss,
            atr=row.entry_atr,
        )
    )
    return Held(
        position_id=row.id,
        position=Position(amount=row.amount, avg_entry=row.avg_entry),
        levels=levels,
        levels_entry=row.levels_entry,
        buy_id=buy.client_order_id,
        plan=StopPlan.from_reason(buy.reason),
        exits=[
            ExitOrder(
                client_order_id=e.client_order_id,
                status=e.status,
                limit=e.price,
                # Written by the bot runner or the kill switch as an ExitCause.
                kind=cast(ExitCause, e.reason["exit"]["kind"]),
            )
            for e in exits
        ],
    )


async def save_levels(
    db: AsyncEngine, position_id: int, avg_entry: Decimal, levels: ExitState
) -> bool:
    """Store the levels, placed from `avg_entry`. False if the position changed meanwhile
    (another fill moved its average, or it closed): then nothing is stored, and the next
    price places them from the position as it is."""
    async with db.begin() as conn:
        result = await conn.execute(
            update(PositionRow)
            .where(
                PositionRow.id == position_id,
                PositionRow.avg_entry == avg_entry,
                PositionRow.closed_at.is_(None),
            )
            .values(
                stop=levels.stop,
                take_profit=levels.take_profit,
                stop_loss=levels.stop_loss,
                peak=levels.peak,
                entry_atr=levels.atr,
                levels_entry=avg_entry,
            )
        )
    return result.rowcount == 1


def levels_json(levels: ExitState | None) -> dict[str, JsonValue] | None:
    """The levels for logs and an exit's `reason`."""
    if levels is None:
        return None
    return {
        "stop": _text(levels.stop),
        "take_profit": _text(levels.take_profit),
        "stop_loss": _text(levels.stop_loss),
        "peak": _text(levels.peak),
        "atr": _text(levels.atr),
    }


# --- What a price does -------------------------------------------------------------------


@dataclass(frozen=True)
class Hold:
    """Nothing to do."""


@dataclass(frozen=True)
class Trail:
    """The trailing stop rose: store `levels`."""

    levels: ExitState


@dataclass(frozen=True)
class Wait:
    """The open exit still reaches the bid: let it fill."""


@dataclass(frozen=True)
class Sell:
    """Sell the whole position: a stop fired, or its exit needs placing again."""

    kind: ExitCause
    again: bool  # False the first time the stop fires


def on_bid(
    levels: ExitState, stops: Stops, bid: Decimal, exits: Sequence[ExitOrder]
) -> Hold | Trail | Wait | Sell:
    """What a new `bid` does to a position with these levels and exits so far."""
    if exits:
        return next_exit(exits, bid)  # a stop fired already: sell until the position is gone
    hit = exit_at_price(levels, bid)
    if hit is not None:
        return Sell(hit.kind, again=False)
    if stops.trailing is None:
        return Hold()  # nothing follows the price
    moved = after_price(levels, stops, bid)
    return Hold() if moved == levels else Trail(moved)


def next_exit(exits: Sequence[ExitOrder], bid: Decimal) -> Wait | Sell:
    """What a new `bid` does to a position that's being sold, given its exits so far: the
    latest is left to fill while it still reaches the bid, and otherwise what's left is
    sold again, for the same cause."""
    if not exits:
        raise ValueError("no exit placed yet: nothing to carry on")
    latest = exits[-1]
    if latest.status == "open" and bid >= latest.limit:
        return Wait()
    return Sell(latest.kind, again=True)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
