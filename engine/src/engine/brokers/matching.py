"""Which paper orders a price update fills, at what price and how much (D34).

The update is MEXC's book ticker: the best bid and ask, and how much is on offer at each.
- An order that crossed the book when it was placed is *taking*: a buy fills at the ask,
  a sell at the bid, on every update that still crosses it. Its limit only caps the
  price. The first update that doesn't cross it leaves it resting on the book.
- A resting order fills at its own limit once the book reaches it: it was there first,
  so the price came to it. When the update is already past the limit, that is worse for
  us than the book, on purpose: we can't tell how it got there.
- Either way it fills at most the size at the best price, so a big order on a thin pair
  fills in parts over several updates. Our orders on one side share that size, best
  price first, then oldest first, as on an exchange.
- The size at a price counts once. The book we see never includes our paper fills, so
  `Taken` remembers how much we took at each side's best price, and only the size beyond
  that is on offer until the best price moves. MEXC re-sends an unchanged quote about
  every 100 ms; without this, the same liquidity would fill again each time.
- No size, or no price on that side: no fill. Better to wait than to invent liquidity.

Pure: no database, no clock.
"""

from collections.abc import Mapping, Sequence
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
    taking: bool  # crossed when placed and hasn't stopped crossing since: fills at the book


@dataclass(frozen=True)
class Match:
    key: str
    price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Taken:
    """How much our orders already filled at one side's best price."""

    price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Matched:
    matches: list[Match]
    taken: dict[OrderSide, Taken]  # to pass to the next match on this symbol


def match(
    orders: Sequence[Resting],
    quote: PriceUpdate,
    *,
    amount_step: Decimal,
    taken: Mapping[OrderSide, Taken] | None = None,
) -> Matched:
    """The fills `quote` gives `orders` (oldest first): buys first, then sells.

    `taken` is what earlier matches on this symbol took (from their `Matched.taken`).
    """
    matches: list[Match] = []
    counts = forget_moved(taken or {}, quote)
    for side in ("buy", "sell"):
        best, size = (quote.ask, quote.ask_size) if side == "buy" else (quote.bid, quote.bid_size)
        if best is None or size is None:
            continue
        already = counts[side].amount if side in counts else Decimal(0)
        available = max(size - already, Decimal(0))
        crossing = [o for o in orders if o.side == side and _crosses(o, best)]
        # sorted() keeps the given (oldest-first) order between equal prices.
        crossing = sorted(crossing, key=lambda o: -o.limit if side == "buy" else o.limit)
        for order in crossing:
            amount = min(order.remaining, available) // amount_step * amount_step
            if amount <= 0:
                break
            price = best if order.taking else order.limit
            matches.append(Match(key=order.key, price=price, amount=amount))
            available -= amount
            already += amount
        counts[side] = Taken(price=best, amount=already)
    return Matched(matches=matches, taken=counts)


def crosses(order: Resting, quote: PriceUpdate) -> bool | None:
    """Whether the book reaches `order`; `None` when `quote` has no price on its side."""
    best = quote.ask if order.side == "buy" else quote.bid
    return None if best is None else _crosses(order, best)


def forget_moved(taken: Mapping[OrderSide, Taken], quote: PriceUpdate) -> dict[OrderSide, Taken]:
    """`taken` without the sides whose best price `quote` shows has moved.

    A new best price is new liquidity, so what we took at the old one stops counting. A
    side `quote` has no price for keeps its count.
    """
    best: dict[OrderSide, Decimal | None] = {"buy": quote.ask, "sell": quote.bid}
    return {side: t for side, t in taken.items() if best[side] is None or best[side] == t.price}


def _crosses(order: Resting, best: Decimal) -> bool:
    return best <= order.limit if order.side == "buy" else best >= order.limit
