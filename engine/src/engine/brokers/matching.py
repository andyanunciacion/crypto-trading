"""Which paper orders a price update fills, at what price and how much (D34).

The update is MEXC's book ticker: the best bid and ask, and how much is on offer at each.
- A new order that already crosses the book is a *taker*: a buy fills at the ask, a
  sell at the bid. Its limit only caps the price.
- An order resting on the book fills at its own limit once the book reaches it: it was
  there first, so the price came to it. When the update is already past the limit, that
  is worse for us than the book, on purpose: we can't tell how it got there.
- Either way it fills at most the size at the best price, so a big order on a thin pair
  fills in parts over several updates. Our orders on one side share that size, best
  price first, then oldest first, as on an exchange.
- No size, or no price on that side: no fill. Better to wait than to invent liquidity.

Pure: no database, no clock.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from engine.exchange.models import OrderSide
from shared.prices import PriceUpdate


@dataclass(frozen=True)
class Resting:
    """An open order, as matching sees it."""

    key: str  # the order's client order ID
    side: OrderSide
    limit: Decimal
    remaining: Decimal  # amount still to fill


@dataclass(frozen=True)
class Match:
    key: str
    price: Decimal
    amount: Decimal


def match(
    orders: Sequence[Resting], quote: PriceUpdate, *, taker: bool, amount_step: Decimal
) -> list[Match]:
    """The fills `quote` gives `orders` (oldest first): buys first, then sells.

    `taker=True` for an order just placed, `False` for orders already on the book.
    """
    matches: list[Match] = []
    for side in ("buy", "sell"):
        best, available = (
            (quote.ask, quote.ask_size) if side == "buy" else (quote.bid, quote.bid_size)
        )
        if best is None or available is None:
            continue
        crossing = [o for o in orders if o.side == side and _crosses(o, best)]
        # sorted() keeps the given (oldest-first) order between equal prices.
        crossing = sorted(crossing, key=lambda o: -o.limit if side == "buy" else o.limit)
        for order in crossing:
            amount = min(order.remaining, available) // amount_step * amount_step
            if amount <= 0:
                break
            price = best if taker else order.limit
            matches.append(Match(key=order.key, price=price, amount=amount))
            available -= amount
    return matches


def _crosses(order: Resting, best: Decimal) -> bool:
    return best <= order.limit if order.side == "buy" else best >= order.limit
