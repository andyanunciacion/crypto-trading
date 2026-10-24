"""Turns a bot's signal into an order (D36). Pure: no database, no exchange, no clock.

A BUY is sized by the bot's sizing mode (risk/sizing.py, D39), exactly as its backtests
size it; a SELL sells the whole position. Orders too small for the market are skipped,
never rounded up (D22).

The price is a *marketable limit*: just past the best price, so the order crosses the
book and fills now, but never at worse than the limit. The buffer is the backtest's
slippage for the pair, so paper trading allows the same price move a backtest assumes.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from engine.exchange.models import MarketRules, OrderRequest, OrderSide
from engine.marketdata.quotes import MAX_QUOTE_AGE
from engine.risk.sizing import NotSized, Sizing, below_minimum, entry_amount
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
) -> Planned | Skip:
    """The order for a BUY or SELL `signal` on `rules.symbol`, or why there is none.

    `cash` is the bot's free quote currency and `equity` all it's worth; `quote` the
    latest price from the stream; `atr` the ATR its sizing needs, if any (`atr_for`).
    """
    if signal.action == "hold":
        raise ValueError("a hold signal has no order")
    side: OrderSide = signal.action
    symbol = rules.symbol
    if side == "buy" and position is not None:
        return Skip("already holding")  # one position at a time, as in a backtest
    if side == "sell" and position is None:
        return Skip("nothing to sell")
    if quote is None:
        return Skip(f"no price for {symbol}")
    age = now - quote.ts
    if age > MAX_QUOTE_AGE:
        return Skip(f"price for {symbol} is {age.total_seconds():.0f} s old")

    detail: str | None = None
    if side == "buy":
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
        amount, detail = sized.amount, sized.detail
    else:
        assert position is not None  # checked above; narrows the type
        if quote.bid is None:
            return Skip(f"no bid for {symbol}")
        # Rounded down, so it still reaches the bid.
        limit = quote.bid * (1 - slippage) // rules.price_step * rules.price_step
        amount = position.amount
        too_small = below_minimum(amount, limit, rules)
        if too_small is not None:
            return Skip(too_small)

    request = OrderRequest(
        symbol=symbol, side=side, amount=amount, price=limit, client_order_id=client_order_id
    )
    return Planned(request, detail)


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
