"""Turns a bot's signal into an order (D36). Pure: no database, no exchange, no clock.

A BUY is sized by the bot's sizing mode (risk/sizing.py, D39), exactly as its backtests
size it, and is skipped while holding unless its strategy scales in (grid, DCA; P6-6).
A SELL sells the whole position, or its `size_hint` share (`sell_amount`), as its
backtests do; a stop's exit sells the whole position (`exit_order`, D40).
Orders too small for the market are skipped, never rounded up (D22). A BUY whose stops
couldn't be placed (an ATR stop with no ATR) is skipped too, as a backtest skips it.

The price is a *marketable limit*: just past the best price, so the order crosses the
book and fills now, but never at worse than the limit. The buffer is the backtest's
slippage for the pair, so paper trading allows the same price move a backtest assumes.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from engine.exchange.models import MarketRules, OrderRequest, OrderSide
from engine.marketdata.quotes import MAX_QUOTE_AGE
from engine.risk.sizing import NotSized, Sizing, entry_amount, sell_amount
from engine.risk.stops import NO_STOPS, NoLevels, Stops, levels_at_entry
from engine.strategies.base import Position, Signal
from shared.prices import PriceUpdate


@dataclass(frozen=True)
class Skip:
    """No order for this signal, and why."""

    reason: str


@dataclass(frozen=True)
class Planned:
    request: OrderRequest
    sizing: str | None  # how a buy's amount came about; None for a sell
    sold: str | None = None  # how a sell of part of the position came about (P6-6)


def order_for(
    signal: Signal,
    position: Position | None,
    cash: Decimal,
    quote: PriceUpdate | None,
    rules: MarketRules,
    *,
    sizing: Sizing,
    equity: Decimal,
    atr: Decimal | None,
    client_order_id: str,
    now: datetime,
    fee_rate: Decimal,
    slippage: Decimal,
    stops: Stops = NO_STOPS,
    stops_atr: Decimal | None = None,
    scales_in: bool = False,
) -> Planned | Skip:
    """The order for a BUY or SELL `signal` on `rules.symbol`, or why there is none.

    `cash` is the bot's free quote currency and `equity` all it's worth; `quote` the
    latest price from the stream; `atr` the ATR its sizing needs, if any (`atr_for`);
    `stops_atr` the one its stops need, if they use ATR. `scales_in`: the strategy whose
    signal it is may add to its position (P6-6).
    """
    if signal.action == "hold":
        raise ValueError("a hold signal has no order")
    side: OrderSide = signal.action
    symbol = rules.symbol
    if side == "buy" and position is not None and not scales_in:
        return Skip("already holding")  # one position at a time, as in a backtest
    if side == "sell" and position is None:
        return Skip("nothing to sell")
    stale = _stale(quote, symbol, now)
    if stale is not None:
        return stale
    assert quote is not None  # _stale checked; narrows the type

    if side == "sell":
        assert position is not None  # checked above; narrows the type
        return _sell(position, quote, rules, client_order_id, slippage, signal.size_hint)
    if quote.ask is None:
        return Skip(f"no ask for {symbol}")
    # Rounded up, so it still reaches the ask.
    limit = _round_up(quote.ask * (1 + slippage), rules.price_step)
    # Sized at the limit, the most it can cost, so the broker's reservation
    # (limit x amount x (1 + fee)) always fits in the cash.
    sized = entry_amount(
        sizing,
        cash=cash,
        equity=equity,
        price=limit,
        fee_rate=fee_rate,
        rules=rules,
        size_hint=signal.size_hint,
        atr=atr,
    )
    if isinstance(sized, NotSized):
        return Skip(sized.reason)
    if stops != NO_STOPS:
        # Placed for real from the fill price; here only whether they can be at all.
        levels = levels_at_entry(stops, limit, stops_atr)
        if isinstance(levels, NoLevels):
            return Skip(levels.reason)
    request = OrderRequest(
        symbol=symbol, side=side, amount=sized.amount, price=limit, client_order_id=client_order_id
    )
    return Planned(request, sized.detail)


def exit_order(
    position: Position,
    quote: PriceUpdate | None,
    rules: MarketRules,
    *,
    client_order_id: str,
    now: datetime,
    slippage: Decimal,
) -> Planned | Skip:
    """The order that sells all of `position` when a stop is hit: priced like a SELL."""
    stale = _stale(quote, rules.symbol, now)
    if stale is not None:
        return stale
    assert quote is not None  # _stale checked; narrows the type
    return _sell(position, quote, rules, client_order_id, slippage)


def _stale(quote: PriceUpdate | None, symbol: str, now: datetime) -> Skip | None:
    if quote is None:
        return Skip(f"no price for {symbol}")
    age = now - quote.ts
    if age > MAX_QUOTE_AGE:
        return Skip(f"price for {symbol} is {age.total_seconds():.0f} s old")
    return None


def _sell(
    position: Position,
    quote: PriceUpdate,
    rules: MarketRules,
    client_order_id: str,
    slippage: Decimal,
    fraction: Decimal | None = None,
) -> Planned | Skip:
    """A sell of the whole position, or of `fraction` of it (a SELL's `size_hint`)."""
    if quote.bid is None:
        return Skip(f"no bid for {rules.symbol}")
    # Rounded down, so it still reaches the bid.
    limit = quote.bid * (1 - slippage) // rules.price_step * rules.price_step
    sold = sell_amount(position.amount, fraction, limit, rules)
    if isinstance(sold, NotSized):
        return Skip(sold.reason)
    request = OrderRequest(
        symbol=rules.symbol,
        side="sell",
        amount=sold.amount,
        price=limit,
        client_order_id=client_order_id,
    )
    return Planned(request, None, None if fraction is None else sold.detail)


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
