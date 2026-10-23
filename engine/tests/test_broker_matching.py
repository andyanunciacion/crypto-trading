"""Which paper orders fill on a price update, at what price and how much (D34).

Pure: no database, no Redis. Numbers are small and round so each case can be checked by hand.
"""

from datetime import UTC, datetime
from decimal import Decimal

from engine.brokers.matching import Match, Resting, match
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


def buy(limit: str, remaining: str = "1", key: str = "a") -> Resting:
    return Resting(key=key, side="buy", limit=Decimal(limit), remaining=Decimal(remaining))


def sell(limit: str, remaining: str = "1", key: str = "a") -> Resting:
    return Resting(key=key, side="sell", limit=Decimal(limit), remaining=Decimal(remaining))


def test_a_new_buy_at_or_above_the_ask_fills_at_the_ask() -> None:
    # A taker buy pays the best ask, not its (higher) limit.
    assert match([buy("101")], quote(), taker=True, amount_step=STEP) == [
        Match(key="a", price=Decimal("100"), amount=Decimal("1"))
    ]


def test_a_new_sell_at_or_below_the_bid_fills_at_the_bid() -> None:
    assert match([sell("98")], quote(), taker=True, amount_step=STEP) == [
        Match(key="a", price=Decimal("99"), amount=Decimal("1"))
    ]


def test_an_order_that_doesnt_cross_does_not_fill() -> None:
    assert match([buy("99.99")], quote(), taker=True, amount_step=STEP) == []
    assert match([sell("100.01")], quote(), taker=False, amount_step=STEP) == []


def test_fills_no_more_than_the_size_at_the_best_price() -> None:
    # 1.5 on offer at the ask, 4 wanted: 1.5 now, the rest stays open.
    matches = match([buy("100", "4")], quote(ask_size="1.5"), taker=True, amount_step=STEP)

    assert matches == [Match(key="a", price=Decimal("100"), amount=Decimal("1.5"))]


def test_a_resting_order_fills_at_its_own_limit() -> None:
    # It was on the book first; the price came to it. Pessimistic when the ask is lower.
    assert match([buy("100.5")], quote(), taker=False, amount_step=STEP) == [
        Match(key="a", price=Decimal("100.5"), amount=Decimal("1"))
    ]
    assert match([sell("98.5")], quote(), taker=False, amount_step=STEP) == [
        Match(key="a", price=Decimal("98.5"), amount=Decimal("1"))
    ]


def test_no_size_or_no_price_means_no_fill() -> None:
    assert match([buy("101")], quote(ask_size=None), taker=True, amount_step=STEP) == []
    assert match([buy("101")], quote(ask=None), taker=True, amount_step=STEP) == []
    assert match([sell("98")], quote(bid_size="0"), taker=True, amount_step=STEP) == []


def test_orders_on_one_side_share_the_size_best_price_first() -> None:
    # 2.5 on offer. The higher buy is first in line, whatever order they came in.
    orders = [buy("100", "2", key="older"), buy("101", "2", key="newer")]

    matches = match(orders, quote(ask_size="2.5"), taker=False, amount_step=STEP)

    assert matches == [
        Match(key="newer", price=Decimal("101"), amount=Decimal("2")),
        Match(key="older", price=Decimal("100"), amount=Decimal("0.5")),
    ]


def test_equal_prices_fill_oldest_first() -> None:
    orders = [sell("99", "1", key="first"), sell("99", "1", key="second")]

    matches = match(orders, quote(bid_size="1"), taker=False, amount_step=STEP)

    assert matches == [Match(key="first", price=Decimal("99"), amount=Decimal("1"))]


def test_buys_and_sells_use_their_own_side_of_the_book() -> None:
    orders = [buy("100", "3", key="b"), sell("99", "3", key="s")]

    matches = match(orders, quote(bid_size="1", ask_size="2"), taker=False, amount_step=STEP)

    assert matches == [
        Match(key="b", price=Decimal("100"), amount=Decimal("2")),
        Match(key="s", price=Decimal("99"), amount=Decimal("1")),
    ]


def test_fill_amounts_round_down_to_the_amount_step() -> None:
    # A size off the step can't make an amount the exchange would refuse later.
    matches = match([buy("100", "2")], quote(ask_size="0.12345"), taker=True, amount_step=STEP)

    assert matches == [Match(key="a", price=Decimal("100"), amount=Decimal("0.123"))]
    assert match([buy("100")], quote(ask_size="0.0009"), taker=True, amount_step=STEP) == []
