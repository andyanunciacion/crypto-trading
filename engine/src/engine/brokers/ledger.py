"""Paper money: what each order and fill does to balances and to the position (D34).

Balances have two parts, as on an exchange: `free` to spend and `used` (held by open
orders). Placing an order *reserves* its funds, moving them from free to used, so two
open orders can never spend the same cash. A buy reserves its limit price x amount plus
the fee, in the quote currency; a sell reserves the coins. Each fill releases its share
of the reservation and settles at the fill price; cancelling releases what's left.

Fees are `fee_rate` of each fill's value, in the quote currency, as in the backtester.

Pure and exact: Decimal throughout, no rounding (an average entry price is the one division).
"""

from dataclasses import dataclass
from decimal import Decimal

from engine.exchange.models import OrderSide


class InsufficientFunds(Exception):
    """Not enough free balance to reserve for an order: the order is rejected."""


@dataclass(frozen=True)
class Funds:
    """One currency's paper balance."""

    free: Decimal = Decimal(0)
    used: Decimal = Decimal(0)  # reserved by open orders


def reservation(side: OrderSide, limit: Decimal, amount: Decimal, fee_rate: Decimal) -> Decimal:
    """What an order for `amount` holds: quote currency for a buy, base currency for a sell.

    A buy never fills above its limit, so this always covers its cost and fee.
    """
    if side == "buy":
        return limit * amount * (1 + fee_rate)
    return amount


def reserve(funds: Funds, amount: Decimal, currency: str) -> Funds:
    if amount > funds.free:
        raise InsufficientFunds(f"needs {amount} {currency}, {funds.free} free")
    return Funds(free=funds.free - amount, used=funds.used + amount)


def release(funds: Funds, amount: Decimal) -> Funds:
    if amount > funds.used:
        raise ValueError(f"releasing {amount}, but only {funds.used} used")
    return Funds(free=funds.free + amount, used=funds.used - amount)


@dataclass(frozen=True)
class Settlement:
    base: Funds  # e.g. BTC
    quote: Funds  # e.g. USDT
    fee: Decimal  # in the quote currency


def settle(
    side: OrderSide,
    limit: Decimal,
    price: Decimal,
    amount: Decimal,
    fee_rate: Decimal,
    *,
    base: Funds,
    quote: Funds,
) -> Settlement:
    """Balances after `amount` of an order at `limit` fills at `price`."""
    if (side == "buy" and price > limit) or (side == "sell" and price < limit):
        raise ValueError(f"a {side} at limit {limit} can't fill at {price}")
    value = price * amount
    fee = value * fee_rate
    held = reservation(side, limit, amount, fee_rate)
    if side == "buy":
        # What was held at the limit, minus what it really cost: a fill below the limit
        # (price improvement) gives the difference back.
        quote = release(quote, held)
        quote = Funds(free=quote.free - value - fee, used=quote.used)
        base = Funds(free=base.free + amount, used=base.used)
    else:
        base = Funds(free=base.free, used=base.used - held)
        if base.used < 0:
            raise ValueError(f"selling {amount}, but only {base.used + held} used")
        quote = Funds(free=quote.free + value - fee, used=quote.used)
    return Settlement(base=base, quote=quote, fee=fee)


@dataclass(frozen=True)
class PositionState:
    """A long spot position. `amount` 0 means it has closed.

    `realized_pnl` counts every fee as soon as it's paid (fees are cash already spent),
    plus (sell price - average entry) x amount on each sell. So once a position closes,
    it's exactly a backtest trade's P&L net of both fees (D30).
    """

    amount: Decimal
    avg_entry: Decimal
    fees_paid: Decimal
    realized_pnl: Decimal


def apply_fill(
    position: PositionState | None,
    side: OrderSide,
    price: Decimal,
    amount: Decimal,
    fee: Decimal,
) -> PositionState:
    """The position after a fill; `None` means flat (a buy then opens one)."""
    held = Decimal(0) if position is None else position.amount
    if side == "sell":
        if position is None or amount > held:
            raise ValueError(f"sells {amount}, holds {held}")
        return PositionState(
            amount=held - amount,
            avg_entry=position.avg_entry,
            fees_paid=position.fees_paid + fee,
            realized_pnl=position.realized_pnl + (price - position.avg_entry) * amount - fee,
        )
    if position is None:
        return PositionState(amount=amount, avg_entry=price, fees_paid=fee, realized_pnl=-fee)
    total = held + amount
    return PositionState(
        amount=total,
        avg_entry=(position.avg_entry * held + price * amount) / total,
        fees_paid=position.fees_paid + fee,
        realized_pnl=position.realized_pnl - fee,
    )
