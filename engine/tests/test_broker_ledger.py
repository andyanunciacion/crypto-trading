"""Paper balances and positions: reservations, fills, fees and P&L (D34). Hand-worked numbers.

Fee rate 0.1% throughout, charged in the quote currency (USDT), as in the backtester.
"""

from decimal import Decimal

import pytest

from engine.brokers.ledger import (
    Funds,
    InsufficientFunds,
    PositionState,
    apply_fill,
    release,
    reservation,
    reserve,
    settle,
)

FEE = Decimal("0.001")


def d(value: str) -> Decimal:
    return Decimal(value)


# --- Reserving funds for an open order -------------------------------------------------


def test_a_buy_reserves_its_cost_plus_the_fee_in_the_quote_currency() -> None:
    assert reservation("buy", d("100"), d("2"), FEE) == d("200.2")


def test_a_sell_reserves_the_coins() -> None:
    assert reservation("sell", d("100"), d("2"), FEE) == d("2")


def test_reserving_moves_free_to_used() -> None:
    assert reserve(Funds(free=d("1000")), d("200.2"), "USDT") == Funds(
        free=d("799.8"), used=d("200.2")
    )


def test_reserving_more_than_is_free_is_refused() -> None:
    with pytest.raises(InsufficientFunds, match=r"needs 200.2 USDT, 150 free"):
        reserve(Funds(free=d("150"), used=d("500")), d("200.2"), "USDT")


def test_releasing_moves_used_back_to_free() -> None:
    assert release(Funds(free=d("799.8"), used=d("200.2")), d("150.15")) == Funds(
        free=d("949.95"), used=d("50.05")
    )


def test_releasing_more_than_is_used_is_a_bug() -> None:
    with pytest.raises(ValueError, match="only 1 used"):
        release(Funds(free=d("0"), used=d("1")), d("2"))


# --- Settling a fill -------------------------------------------------------------------


def test_a_buy_fill_pays_price_and_fee_and_refunds_the_price_improvement() -> None:
    # Reserved 200.2 at the 100 limit; filled at 99.5: cost 199, fee 0.199.
    quote = Funds(free=d("799.8"), used=d("200.2"))

    result = settle("buy", d("100"), d("99.5"), d("2"), FEE, base=Funds(), quote=quote)

    assert result.fee == d("0.199")
    assert result.quote == Funds(free=d("800.801"), used=d("0"))  # 1000 - 199 - 0.199
    assert result.base == Funds(free=d("2"), used=d("0"))


def test_a_partial_buy_fill_releases_only_its_share_of_the_reservation() -> None:
    quote = Funds(free=d("799.8"), used=d("200.2"))

    result = settle("buy", d("100"), d("99.5"), d("0.5"), FEE, base=Funds(), quote=quote)

    # Releases 50.05 of the 200.2; pays 49.75 + 0.04975 fee.
    assert result.quote == Funds(free=d("800.05025"), used=d("150.15"))
    assert result.base == Funds(free=d("0.5"))


def test_a_sell_fill_receives_the_proceeds_minus_the_fee() -> None:
    base = Funds(free=d("0"), used=d("2"))

    result = settle("sell", d("110"), d("111"), d("2"), FEE, base=base, quote=Funds(free=d("10")))

    assert result.fee == d("0.222")
    assert result.base == Funds(free=d("0"), used=d("0"))
    assert result.quote == Funds(free=d("231.778"), used=d("0"))  # 10 + 222 - 0.222


def test_a_fill_beyond_its_limit_is_a_bug() -> None:
    with pytest.raises(ValueError, match="limit"):
        settle("buy", d("100"), d("100.01"), d("1"), FEE, base=Funds(), quote=Funds(used=d("101")))
    with pytest.raises(ValueError, match="limit"):
        settle("sell", d("100"), d("99.99"), d("1"), FEE, base=Funds(used=d("1")), quote=Funds())


# --- Positions -------------------------------------------------------------------------


def test_a_first_buy_opens_a_position_with_its_fee_already_counted() -> None:
    position = apply_fill(None, "buy", d("99.5"), d("2"), d("0.199"))

    assert position == PositionState(
        amount=d("2"), avg_entry=d("99.5"), fees_paid=d("0.199"), realized_pnl=d("-0.199")
    )


def test_buys_average_the_entry_price_by_amount() -> None:
    position = apply_fill(None, "buy", d("100"), d("1"), d("0.1"))
    position = apply_fill(position, "buy", d("104"), d("3"), d("0.312"))

    assert position.amount == d("4")
    assert position.avg_entry == d("103")  # (100 + 312) / 4
    assert position.fees_paid == d("0.412")


def test_a_round_trip_realizes_pnl_net_of_both_fees() -> None:
    # Same as a backtest trade (D30): (111 - 99.5) x 2 - 0.199 - 0.222.
    position = apply_fill(None, "buy", d("99.5"), d("2"), d("0.199"))
    position = apply_fill(position, "sell", d("111"), d("2"), d("0.222"))

    assert position.amount == d("0")
    assert position.realized_pnl == d("22.579")
    assert position.fees_paid == d("0.421")
    assert position.avg_entry == d("99.5")  # kept for the record after closing


def test_partial_sells_realize_pnl_on_what_was_sold() -> None:
    position = apply_fill(None, "buy", d("100"), d("2"), d("0.2"))
    position = apply_fill(position, "sell", d("110"), d("0.5"), d("0.055"))

    assert position.amount == d("1.5")
    assert position.realized_pnl == d("4.745")  # -0.2 + 10 x 0.5 - 0.055
    assert position.avg_entry == d("100")


def test_selling_more_than_held_is_a_bug() -> None:
    position = apply_fill(None, "buy", d("100"), d("1"), d("0.1"))

    with pytest.raises(ValueError, match="sells 2, holds 1"):
        apply_fill(position, "sell", d("110"), d("2"), d("0.22"))
    with pytest.raises(ValueError, match="sells 1, holds 0"):
        apply_fill(None, "sell", d("110"), d("1"), d("0.11"))
