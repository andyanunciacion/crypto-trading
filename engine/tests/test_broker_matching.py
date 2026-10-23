"""Which paper orders fill on a price update, at what price and how much (D34).

Pure: no database, no Redis. Numbers are small and round so each case can be checked by hand.
"""

from datetime import UTC, datetime
from decimal import Decimal

from engine.brokers.matching import Match, Resting, Taken, crosses, forget_moved, match
from engine.exchange.models import OrderSide
from shared.prices import PriceUpdate

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
STEP = Decimal("0.001")


def quote(
    bid: str | None = "99",
    ask: str | None = "100",
    bid_size: str | None = "5",
    ask_size: str | None = "5",
) -> PriceUpdate:
    def dec(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    return PriceUpdate(
        exchange="mexc",
        symbol="BTC/USDT",
        bid=dec(bid),
        ask=dec(ask),
        bid_size=dec(bid_size),
        ask_size=dec(ask_size),
        ts=T0,
        received_at=T0,
        source="ws",
    )


def buy(limit: str, remaining: str = "1", key: str = "a", *, taking: bool = False) -> Resting:
    return Resting(
        key=key, side="buy", limit=Decimal(limit), remaining=Decimal(remaining), taking=taking
    )


def sell(limit: str, remaining: str = "1", key: str = "a", *, taking: bool = False) -> Resting:
    return Resting(
        key=key, side="sell", limit=Decimal(limit), remaining=Decimal(remaining), taking=taking
    )


def taken(price: str, amount: str) -> Taken:
    return Taken(price=Decimal(price), amount=Decimal(amount))


# --- Price: takers pay the book, resting orders their limit ----------------------------


def test_a_taking_buy_at_or_above_the_ask_fills_at_the_ask() -> None:
    # A taker buy pays the best ask, not its (higher) limit.
    assert match([buy("101", taking=True)], quote(), amount_step=STEP).matches == [
        Match(key="a", price=Decimal("100"), amount=Decimal("1"))
    ]


def test_a_taking_sell_at_or_below_the_bid_fills_at_the_bid() -> None:
    assert match([sell("98", taking=True)], quote(), amount_step=STEP).matches == [
        Match(key="a", price=Decimal("99"), amount=Decimal("1"))
    ]


def test_a_taking_remainder_fills_at_the_new_ask_not_its_limit() -> None:
    # Placed with a 101 limit when the ask was 100; the ask is 100.5 now. Still crossing,
    # so it keeps taking from the book: 100.5, not the 101 it would allow.
    assert match([buy("101", taking=True)], quote(ask="100.5"), amount_step=STEP).matches == [
        Match(key="a", price=Decimal("100.5"), amount=Decimal("1"))
    ]


def test_a_resting_order_fills_at_its_own_limit() -> None:
    # It was on the book first; the price came to it. Pessimistic when the ask is lower.
    assert match([buy("101")], quote(ask="100.5"), amount_step=STEP).matches == [
        Match(key="a", price=Decimal("101"), amount=Decimal("1"))
    ]
    assert match([sell("98.5")], quote(), amount_step=STEP).matches == [
        Match(key="a", price=Decimal("98.5"), amount=Decimal("1"))
    ]


def test_an_order_that_doesnt_cross_does_not_fill() -> None:
    assert match([buy("99.99", taking=True)], quote(), amount_step=STEP).matches == []
    assert match([sell("100.01")], quote(), amount_step=STEP).matches == []


# --- Size: at most what's on offer, and only once ----------------------------------------


def test_fills_no_more_than_the_size_at_the_best_price() -> None:
    # 1.5 on offer at the ask, 4 wanted: 1.5 now, the rest stays open.
    matched = match([buy("100", "4", taking=True)], quote(ask_size="1.5"), amount_step=STEP)

    assert matched.matches == [Match(key="a", price=Decimal("100"), amount=Decimal("1.5"))]
    assert matched.taken["buy"] == taken("100", "1.5")


def test_an_unchanged_quote_offers_nothing_more() -> None:
    # MEXC re-sends the same best ask every ~100 ms. We took its 1.5 already.
    matched = match(
        [buy("100", "2.5", taking=True)],
        quote(ask_size="1.5"),
        amount_step=STEP,
        taken={"buy": taken("100", "1.5")},
    )

    assert matched.matches == []
    assert matched.taken["buy"] == taken("100", "1.5")


def test_a_grown_size_at_the_same_price_offers_only_the_difference() -> None:
    # 0.5 taken at 100; the ask now shows 0.8 there: 0.3 is new.
    matched = match(
        [buy("100", "1", taking=True)],
        quote(ask_size="0.8"),
        amount_step=STEP,
        taken={"buy": taken("100", "0.5")},
    )

    assert matched.matches == [Match(key="a", price=Decimal("100"), amount=Decimal("0.3"))]
    assert matched.taken["buy"] == taken("100", "0.8")


def test_a_shrunk_size_at_the_same_price_offers_nothing() -> None:
    # 0.5 taken at 100; only 0.4 shows there now. Our 0.5 would be gone from it already.
    matched = match(
        [buy("100", "1", taking=True)],
        quote(ask_size="0.4"),
        amount_step=STEP,
        taken={"buy": taken("100", "0.5")},
    )

    assert matched.matches == []


def test_a_new_best_price_offers_its_full_size() -> None:
    # Everything at 100 was taken; the ask moved to 100.5 with 0.6 there.
    matched = match(
        [buy("101", "1", taking=True)],
        quote(ask="100.5", ask_size="0.6"),
        amount_step=STEP,
        taken={"buy": taken("100", "5")},
    )

    assert matched.matches == [Match(key="a", price=Decimal("100.5"), amount=Decimal("0.6"))]
    assert matched.taken["buy"] == taken("100.5", "0.6")


def test_each_side_keeps_its_own_count() -> None:
    # The buy side's 100 is used up; the sell side's 99 isn't.
    matched = match(
        [buy("100", "1", key="b", taking=True), sell("99", "1", key="s", taking=True)],
        quote(),
        amount_step=STEP,
        taken={"buy": taken("100", "5")},
    )

    assert matched.matches == [Match(key="s", price=Decimal("99"), amount=Decimal("1"))]
    assert matched.taken == {"buy": taken("100", "5"), "sell": taken("99", "1")}


def test_a_side_without_a_price_keeps_its_count() -> None:
    matched = match([], quote(ask=None), amount_step=STEP, taken={"buy": taken("100", "1")})

    assert matched.taken["buy"] == taken("100", "1")


def test_no_size_or_no_price_means_no_fill() -> None:
    order = buy("101", taking=True)
    assert match([order], quote(ask_size=None), amount_step=STEP).matches == []
    assert match([order], quote(ask=None), amount_step=STEP).matches == []
    assert match([sell("98", taking=True)], quote(bid_size="0"), amount_step=STEP).matches == []


def test_orders_on_one_side_share_the_size_best_price_first() -> None:
    # 2.5 on offer. The higher buy is first in line, whatever order they came in.
    orders = [buy("100", "2", key="older"), buy("101", "2", key="newer")]

    matched = match(orders, quote(ask_size="2.5"), amount_step=STEP)

    assert matched.matches == [
        Match(key="newer", price=Decimal("101"), amount=Decimal("2")),
        Match(key="older", price=Decimal("100"), amount=Decimal("0.5")),
    ]


def test_orders_share_what_is_left_after_earlier_fills() -> None:
    # 3 on offer, 1 of it taken already: the two orders share the other 2.
    orders = [buy("100", "1.5", key="first"), buy("100", "1.5", key="second")]

    matched = match(orders, quote(ask_size="3"), amount_step=STEP, taken={"buy": taken("100", "1")})

    assert matched.matches == [
        Match(key="first", price=Decimal("100"), amount=Decimal("1.5")),
        Match(key="second", price=Decimal("100"), amount=Decimal("0.5")),
    ]
    assert matched.taken["buy"] == taken("100", "3")


def test_equal_prices_fill_oldest_first() -> None:
    orders = [sell("99", "1", key="first"), sell("99", "1", key="second")]

    matched = match(orders, quote(bid_size="1"), amount_step=STEP)

    assert matched.matches == [Match(key="first", price=Decimal("99"), amount=Decimal("1"))]


def test_buys_and_sells_use_their_own_side_of_the_book() -> None:
    orders = [buy("100", "3", key="b"), sell("99", "3", key="s")]

    matched = match(orders, quote(bid_size="1", ask_size="2"), amount_step=STEP)

    assert matched.matches == [
        Match(key="b", price=Decimal("100"), amount=Decimal("2")),
        Match(key="s", price=Decimal("99"), amount=Decimal("1")),
    ]


def test_fill_amounts_round_down_to_the_amount_step() -> None:
    # A size off the step can't make an amount the exchange would refuse later.
    matched = match([buy("100", "2")], quote(ask_size="0.12345"), amount_step=STEP)

    assert matched.matches == [Match(key="a", price=Decimal("100"), amount=Decimal("0.123"))]
    assert matched.taken["buy"] == taken("100", "0.123")
    assert match([buy("100")], quote(ask_size="0.0009"), amount_step=STEP).matches == []


# --- Helpers the broker uses between matches ---------------------------------------------


def test_crosses_says_whether_the_book_reaches_the_order() -> None:
    assert crosses(buy("100"), quote(ask="100")) is True
    assert crosses(buy("100"), quote(ask="100.01")) is False
    assert crosses(sell("99"), quote(bid="99")) is True
    assert crosses(sell("99"), quote(bid="98.99")) is False
    # No price on its side: the quote can't tell.
    assert crosses(buy("100"), quote(ask=None)) is None


def test_forget_moved_drops_counts_for_a_price_that_moved() -> None:
    counts: dict[OrderSide, Taken] = {"buy": taken("100", "1"), "sell": taken("99", "2")}

    # The ask moved, the bid didn't.
    assert forget_moved(counts, quote(ask="100.5")) == {"sell": taken("99", "2")}
    # A side without a price says nothing about it.
    assert forget_moved(counts, quote(ask=None)) == counts
