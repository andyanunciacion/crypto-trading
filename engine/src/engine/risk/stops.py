"""Stops: when an open position is sold whatever the strategy says (ARCHITECTURE §6, D40).

Pure, all Decimal. Three kinds, any of them, set when the position opens:
- a stop-loss below the entry, so a trade that goes wrong loses a known amount;
- a take-profit above it, which banks a gain at a set price;
- a trailing stop below the highest price since the entry. It rises with the price and
  never falls, so it keeps part of a gain once the price turns.

Distances are a percent, or a multiple of ATR (how far the price typically moves in one
candle, so a wilder market gets a wider stop). A take-profit can also be an R multiple:
R is what the stop-loss risks per coin (entry minus stop), so "2R" sits twice that far
above the entry.

A backtest only has candles, not the order prices came in, so it checks each candle
pessimistically (`exit_on_candle`): a price that gapped past a level fills at the open;
with a stop and a take-profit both inside one candle, the stop is assumed to have come
first; and a candle's own high lifts the trailing stop only after the candle
(`after_candle`), never into the low of that same candle. Paper and live bots check
every price instead (`exit_at_price`, `after_price`): one price comes in one order, so
there's nothing to assume. They check the bid, which is what a sell gets.
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from engine.exchange.models import Candle
from engine.risk.sizing import atr_lookback


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Percent(_Frozen):
    kind: Literal["percent"] = "percent"
    percent: Decimal = Field(gt=0, lt=100, description="distance, as a percent of the price")


class AtrMultiple(_Frozen):
    kind: Literal["atr"] = "atr"
    multiple: Decimal = Field(gt=0, description="distance, in ATRs")


class RMultiple(_Frozen):
    kind: Literal["r"] = "r"
    multiple: Decimal = Field(gt=0, description="distance, in multiples of the stop-loss's")


StopDistance = Annotated[Percent | AtrMultiple, Field(discriminator="kind")]
TakeProfitDistance = Annotated[Percent | RMultiple, Field(discriminator="kind")]


class Stops(_Frozen):
    """Which stops a position gets. `Stops()` (`{}` as JSON) means none."""

    stop_loss: StopDistance | None = None  # below the entry
    take_profit: TakeProfitDistance | None = None  # above the entry
    trailing: StopDistance | None = None  # below the highest price since the entry
    atr_period: int = Field(default=14, ge=1, description="for the ATR distances")

    @model_validator(mode="after")
    def _r_needs_a_stop_loss(self) -> Self:
        if isinstance(self.take_profit, RMultiple) and self.stop_loss is None:
            raise ValueError("an R-multiple take-profit needs a stop_loss to measure R by")
        return self

    @property
    def uses_atr(self) -> bool:
        return any(isinstance(d, AtrMultiple) for d in (self.stop_loss, self.trailing))


STOPS: TypeAdapter[Stops] = TypeAdapter(Stops)
NO_STOPS = Stops()

ExitKind = Literal["stop_loss", "trailing_stop", "take_profit"]


@dataclass(frozen=True)
class ExitState:
    """An open position's exit levels. `stop` is the higher of the two stops."""

    stop: Decimal | None
    take_profit: Decimal | None
    peak: Decimal  # the highest price since the entry, for the trailing stop
    stop_loss: Decimal | None  # the fixed stop-loss level, to tell which stop fired
    atr: Decimal | None  # the ATR at entry: an ATR trailing stop keeps its distance


@dataclass(frozen=True)
class Exit:
    kind: ExitKind
    price: Decimal  # before slippage


@dataclass(frozen=True)
class NoLevels:
    """The stops can't be placed, so the position shouldn't be opened."""

    reason: str


def lookback(stops: Stops) -> int:
    """How many closed candles the stops need: 0 unless one uses ATR."""
    return atr_lookback(stops.atr_period) if stops.uses_atr else 0


def levels_at_entry(stops: Stops, entry: Decimal, atr: Decimal | None) -> ExitState | NoLevels:
    """The exit levels for a position opened at `entry`, given the ATR when it opened."""
    if stops.uses_atr:
        if atr is None:
            return NoLevels(f"no ATR for the stops: needs {lookback(stops)} candles in a row")
        if atr <= 0:
            return NoLevels("ATR is 0: no price movement to set a stop by")
    stop_loss = None if stops.stop_loss is None else _below(entry, stops.stop_loss, atr)
    take_profit: Decimal | None = None
    match stops.take_profit:
        case Percent(percent=percent):
            take_profit = entry * (1 + percent / 100)
        case RMultiple(multiple=multiple):
            assert stop_loss is not None  # the settings check makes sure of it
            take_profit = entry + multiple * (entry - stop_loss)
    state = ExitState(
        stop=stop_loss, take_profit=take_profit, peak=entry, stop_loss=stop_loss, atr=atr
    )
    return _trail(state, stops)


def exit_on_candle(state: ExitState, candle: Candle) -> Exit | None:
    """Whether `candle` hits a level, and at what price, assuming the worst order."""
    stop, take_profit = state.stop, state.take_profit
    if stop is not None and candle.open <= stop:
        return Exit(_stop_kind(state), candle.open)  # gapped through: the open is all there is
    if take_profit is not None and candle.open >= take_profit:
        return Exit("take_profit", candle.open)
    # The stop first: if both are inside the candle, which came first can't be told.
    if stop is not None and candle.low <= stop:
        return Exit(_stop_kind(state), stop)
    if take_profit is not None and candle.high >= take_profit:
        return Exit("take_profit", take_profit)
    return None


def after_candle(state: ExitState, stops: Stops, candle: Candle) -> ExitState:
    """The levels for the next candle: the trailing stop follows this one's high."""
    return after_price(state, stops, candle.high)


def exit_at_price(state: ExitState, bid: Decimal) -> Exit | None:
    """Whether a live `bid` reaches a level. The exit's price is the bid itself."""
    if state.stop is not None and bid <= state.stop:
        return Exit(_stop_kind(state), bid)
    if state.take_profit is not None and bid >= state.take_profit:
        return Exit("take_profit", bid)
    return None


def after_price(state: ExitState, stops: Stops, price: Decimal) -> ExitState:
    """The levels after `price`: a new high lifts the trailing stop, never lowers it."""
    if price <= state.peak:
        return state
    return _trail(replace(state, peak=price), stops)


def reentry(state: ExitState, stops: Stops, entry: Decimal) -> ExitState | NoLevels:
    """The levels again, for a position whose average entry moved to `entry` (its buy
    filled once more). They keep the ATR at entry, and the trailing stop the peak so far."""
    placed = levels_at_entry(stops, entry, state.atr)
    if isinstance(placed, NoLevels):
        return placed
    return after_price(placed, stops, state.peak)


def _trail(state: ExitState, stops: Stops) -> ExitState:
    """Lift the stop to the trailing level below the peak, if that's higher; never lower."""
    if stops.trailing is None:
        return state
    trail = _below(state.peak, stops.trailing, state.atr)
    if state.stop is not None and state.stop >= trail:
        return state
    return replace(state, stop=trail)


def _below(price: Decimal, distance: Percent | AtrMultiple, atr: Decimal | None) -> Decimal:
    match distance:
        case Percent(percent=percent):
            return price * (1 - percent / 100)
        case AtrMultiple(multiple=multiple):
            assert atr is not None  # levels_at_entry checked
            return price - multiple * atr


def _stop_kind(state: ExitState) -> ExitKind:
    return "stop_loss" if state.stop == state.stop_loss else "trailing_stop"
