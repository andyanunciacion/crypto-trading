"""Signal → order, as the bot runner sizes it until P4: all-in buys, whole-position sells,
priced as a marketable limit. Each expected number is worked out by hand beside it.

Fee 0.1%, slippage 0.05%, amount step 0.0001 BTC, price step 0.01 USDT.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.bots.orders import Skip, order_for
from engine.exchange.models import MarketRules, OrderRequest
from engine.strategies.base import Action, Position, Signal
from shared.prices import PriceUpdate

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=2)  # about when the runner acts on the candle that opened at T0
BTC = "BTC/USDT"
FEE = Decimal("0.001")
SLIPPAGE = Decimal("0.0005")
RULES = MarketRules(
    symbol=BTC,
    amount_step=Decimal("0.0001"),
    price_step=Decimal("0.01"),
    min_amount=Decimal("0.0001"),
    min_cost=Decimal("1"),
)
HOLDING = Position(amount=Decimal("0.5"), avg_entry=Decimal(90))


def d(value: str) -> Decimal:
    return Decimal(value)


def signal(action: Action, size_hint: str | None = None) -> Signal:
    return Signal(
        action=action,
        ts=T0,
        reason="test",
        size_hint=None if size_hint is None else d(size_hint),
    )


def quote(ask: str | None = "100", bid: str | None = "99.9", ts: datetime = NOW) -> PriceUpdate:
    return PriceUpdate(
        exchange="mexc",
        symbol=BTC,
        ask=None if ask is None else d(ask),
        bid=None if bid is None else d(bid),
        ask_size=d("5"),
        bid_size=d("5"),
        ts=ts,
        received_at=ts,
        source="ws",
    )


def order(
    sig: Signal,
    position: Position | None = None,
    cash: str = "1000",
    price: PriceUpdate | None = None,
) -> OrderRequest | Skip:
    side = "b" if sig.action == "buy" else "s"
    return order_for(
        sig,
        position,
        d(cash),
        quote() if price is None else price,
        RULES,
        client_order_id=f"b1-202610231200-{side}",
        now=NOW,
        fee_rate=FEE,
        slippage=SLIPPAGE,
    )


def request(side: str, amount: str, price: str) -> OrderRequest:
    return OrderRequest.model_validate(
        {
            "symbol": BTC,
            "side": side,
            "amount": amount,
            "price": price,
            "client_order_id": f"b1-202610231200-{side[0]}",
        }
    )


# --- Buys ------------------------------------------------------------------------------


def test_a_buy_spends_all_the_cash_at_a_limit_just_above_the_ask() -> None:
    # Limit 100 x 1.0005 = 100.05. Amount 1000 / (100.05 x 1.001) = 9.98502... -> 9.9850.
    assert order(signal("buy")) == request("buy", "9.9850", "100.05")


def test_a_buy_limit_rounds_up_to_the_price_step() -> None:
    # 100.03 x 1.0005 = 100.080015 -> up to 100.09, so the order still crosses the ask.
    # Amount 1000 / (100.09 x 1.001) = 9.98102... -> 9.9810.
    assert order(signal("buy"), price=quote(ask="100.03")) == request("buy", "9.9810", "100.09")


@pytest.mark.parametrize("cash", ["1000", "123.45", "7.77", "1.01"])
def test_a_buy_never_needs_more_than_the_cash(cash: str) -> None:
    # What the PaperBroker reserves: limit x amount x (1 + fee). It must fit in the cash,
    # or the order would be rejected for insufficient funds.
    placed = order(signal("buy"), cash=cash)

    assert isinstance(placed, OrderRequest)
    assert placed.price * placed.amount * (1 + FEE) <= d(cash)
    assert placed.amount % RULES.amount_step == 0


def test_a_size_hint_spends_that_share_of_the_cash() -> None:
    # 500 / (100.05 x 1.001) = 4.99251... -> 4.9925.
    assert order(signal("buy", size_hint="0.5")) == request("buy", "4.9925", "100.05")


def test_no_buy_while_holding() -> None:
    assert order(signal("buy"), position=HOLDING) == Skip("already holding")


def test_no_buy_without_an_ask() -> None:
    assert order(signal("buy"), price=quote(ask=None)) == Skip("no ask for BTC/USDT")


def test_a_buy_below_the_minimum_cost_is_skipped() -> None:
    # 0.5 / (100.05 x 1.001) = 0.00499... -> 0.0049, worth 0.0049 x 100.05 = 0.490245 < 1.
    assert order(signal("buy"), cash="0.5") == Skip("value 0.490245 below min_cost 1")


def test_a_buy_too_small_for_one_step_is_skipped() -> None:
    assert order(signal("buy"), cash="0.001") == Skip("size rounds to 0 at amount step 0.0001")


# --- Sells -----------------------------------------------------------------------------


def test_a_sell_sells_the_whole_position_at_a_limit_just_below_the_bid() -> None:
    # 99.9 x 0.9995 = 99.85005 -> down to 99.85, so the order still crosses the bid.
    assert order(signal("sell"), position=HOLDING) == request("sell", "0.5", "99.85")


def test_a_sell_limit_on_the_step_stays_put() -> None:
    # 100 x 0.9995 = 99.95 exactly.
    placed = order(signal("sell"), position=HOLDING, price=quote(bid="100"))

    assert placed == request("sell", "0.5", "99.95")


def test_no_sell_while_flat() -> None:
    assert order(signal("sell")) == Skip("nothing to sell")


def test_no_sell_without_a_bid() -> None:
    assert order(signal("sell"), position=HOLDING, price=quote(bid=None)) == Skip(
        "no bid for BTC/USDT"
    )


def test_a_sell_below_the_minimum_amount_is_skipped() -> None:
    dust = Position(amount=d("0.00005"), avg_entry=d("90"))

    assert order(signal("sell"), position=dust) == Skip("amount 0.00005 below min_amount 0.0001")


# --- Prices ----------------------------------------------------------------------------


def test_no_order_without_a_price() -> None:
    placed = order_for(
        signal("buy"),
        None,
        d("1000"),
        None,
        RULES,
        client_order_id="b1-202610231200-b",
        now=NOW,
        fee_rate=FEE,
        slippage=SLIPPAGE,
    )

    assert placed == Skip("no price for BTC/USDT")


def test_no_order_on_a_stale_price() -> None:
    stale = quote(ts=NOW - timedelta(seconds=11))

    assert order(signal("buy"), price=stale) == Skip("price for BTC/USDT is 11 s old")


def test_a_price_exactly_10_seconds_old_is_still_fresh() -> None:
    assert isinstance(
        order(signal("buy"), price=quote(ts=NOW - timedelta(seconds=10))), OrderRequest
    )


def test_a_hold_is_not_an_order() -> None:
    with pytest.raises(ValueError, match="hold"):
        order(signal("hold"))
