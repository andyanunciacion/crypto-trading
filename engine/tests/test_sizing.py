"""Position sizing: how much each entry buys, in each mode (risk/sizing.py, D39).

Unless a test says otherwise: price 100, fee 0.1%, 1000 free cash and 1000 equity, amount
step 0.0001, min amount 0.0001, min cost 1. Every number is worked out by hand beside it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle, MarketRules
from engine.risk.sizing import (
    SIZING,
    AllIn,
    AtrRisk,
    FixedAmount,
    NotSized,
    PercentOfEquity,
    Sized,
    Sizing,
    atr_for,
    buy_amount,
    entry_amount,
    lookback,
)

D = Decimal
RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=D("0.0001"),
    price_step=D("0.01"),
    min_amount=D("0.0001"),
    min_cost=D("1"),
)


def size(sizing: Sizing, **overrides: Any) -> Sized | NotSized:
    values: dict[str, Any] = {
        "cash": D(1000),
        "equity": D(1000),
        "price": D(100),
        "fee_rate": D("0.001"),
        "rules": RULES,
        "size_hint": None,
        "atr": None,
    }
    return entry_amount(sizing, **(values | overrides))


def amount(result: Sized | NotSized) -> Decimal:
    assert isinstance(result, Sized), result
    return result.amount


def refused(result: Sized | NotSized) -> str:
    assert isinstance(result, NotSized), result
    return result.reason


# --- The modes ---------------------------------------------------------------------------


def test_all_in_spends_the_free_cash_leaving_room_for_the_fee() -> None:
    # 1000 / (100 x 1.001) = 9.99000999..., down to the 0.0001 step.
    assert amount(size(AllIn())) == D("9.9900")
    # The bot runner's numbers: 1000 / (110.06 x 1.001) = 9.07688...
    assert amount(size(AllIn(), price=D("110.06"))) == D("9.0768")
    # Half of it: 500 / 110.17006 = 4.53848...
    assert amount(size(AllIn(), price=D("110.06"), size_hint=D("0.5"))) == D("4.5384")


def test_all_in_ignores_equity() -> None:
    # What there is to spend is the free cash, however much the bot is worth.
    assert amount(size(AllIn(), equity=D(5000))) == D("9.9900")


def test_fixed_buys_that_value() -> None:
    # 250 / 100 = 2.5 (the fee comes on top: 0.25).
    assert amount(size(FixedAmount(amount=D(250)))) == D("2.5")
    assert amount(size(FixedAmount(amount=D(250)), size_hint=D("0.5"))) == D("1.25")


def test_an_amount_rounds_down_to_the_step() -> None:
    # 100 / 30 = 3.3333..., down (never up) to 3.3333.
    assert amount(size(FixedAmount(amount=D(100)), price=D(30))) == D("3.3333")


def test_percent_of_equity_uses_equity_not_cash() -> None:
    # 25% of 1200 equity = 300; 300 / 100 = 3, though only 500 of it is free cash.
    result = size(PercentOfEquity(percent=D(25)), cash=D(500), equity=D(1200))
    assert amount(result) == D(3)
    # A fifth of that with a 0.2 hint: 60 / 100 = 0.6.
    hinted = size(PercentOfEquity(percent=D(25)), equity=D(1200), size_hint=D("0.2"))
    assert amount(hinted) == D("0.6")


def test_atr_risk_loses_the_risk_percent_at_the_stop() -> None:
    # 1% of 1000 = 10 at risk. The stop sits 2 x ATR 5 = 10 below the entry, so 10 / 10 = 1:
    # stopped at 90, 1 x (100 - 90) = 10 is lost, 1% of equity.
    assert amount(size(AtrRisk(), atr=D(5))) == D(1)
    # Twice as volatile: half the size. 10 / (2 x 10) = 0.5.
    assert amount(size(AtrRisk(), atr=D(10))) == D("0.5")
    # 2% at risk, 3 x ATR 4 = 12 stop: 20 / 12 = 1.6666... -> 1.6666.
    wider = AtrRisk(risk_percent=D(2), multiple=D(3))
    assert amount(size(wider, atr=D(4))) == D("1.6666")


def test_atr_risk_needs_an_atr() -> None:
    assert "no ATR" in refused(size(AtrRisk(), atr=None))
    # A flat market gives no distance to put a stop at.
    assert "ATR is 0" in refused(size(AtrRisk(), atr=D(0)))


# --- Limits of every mode ----------------------------------------------------------------


def test_no_mode_spends_more_than_the_free_cash() -> None:
    # 2000 wanted, 1000 free: capped at what all-in would buy, 9.9900.
    capped = size(FixedAmount(amount=D(2000)))
    assert amount(capped) == D("9.9900")
    assert isinstance(capped, Sized) and "capped by cash" in capped.detail
    # 1% at risk with a tight stop (2 x ATR 0.5 = 1): 10 / 1 = 10 wanted, 9.9900 affordable.
    assert amount(size(AtrRisk(), atr=D("0.5"))) == D("9.9900")
    assert amount(size(PercentOfEquity(percent=D(100)), equity=D(3000))) == D("9.9900")


def test_too_small_is_refused_never_rounded_up() -> None:
    # 0.5 / 100 = 0.005: worth 0.5, under the 1 minimum cost.
    assert "below min_cost" in refused(size(FixedAmount(amount=D("0.5"))))
    # The same, where the market wants at least 0.01.
    strict = RULES.model_copy(update={"min_amount": D("0.01"), "min_cost": None})
    assert "below min_amount" in refused(size(FixedAmount(amount=D("0.5")), rules=strict))
    # 0.001 / 100 = 0.00001, under one 0.0001 step.
    assert "rounds to 0" in refused(size(FixedAmount(amount=D("0.001"))))


def test_no_cash_means_no_buy() -> None:
    assert "rounds to 0" in refused(size(PercentOfEquity(percent=D(10)), cash=D(0)))


def test_the_detail_says_how_the_amount_came_about() -> None:
    result = size(PercentOfEquity(percent=D(25)), equity=D(1200), cash=D(500))
    assert isinstance(result, Sized)
    assert result.detail.startswith("percent_of_equity: 25% of 1200 equity")
    assert "capped" not in result.detail


# --- Settings ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sizing",
    [
        AllIn(),
        FixedAmount(amount=D("250.5")),
        PercentOfEquity(percent=D("12.5")),
        AtrRisk(),
        AtrRisk(risk_percent=D("0.5"), period=20, multiple=D("1.5")),
    ],
)
def test_each_mode_round_trips_through_json(sizing: Sizing) -> None:
    assert SIZING.validate_json(SIZING.dump_json(sizing)) == sizing


def test_the_mode_picks_the_settings() -> None:
    assert SIZING.validate_python({"mode": "fixed", "amount": "100"}) == FixedAmount(amount=D(100))
    assert SIZING.validate_python({"mode": "atr_risk"}) == AtrRisk(
        risk_percent=D(1), period=14, multiple=D(2)
    )


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "martingale"},
        {"mode": "fixed"},
        {"mode": "fixed", "amount": "0"},
        {"mode": "fixed", "amount": "-5"},
        {"mode": "percent_of_equity", "percent": "0"},
        {"mode": "percent_of_equity", "percent": "101"},
        {"mode": "atr_risk", "period": 0},
        {"mode": "atr_risk", "multiple": "0"},
        {"mode": "atr_risk", "risk_percent": "150"},
        {"mode": "all_in", "fraction": "0.5"},
    ],
)
def test_bad_settings_are_refused(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SIZING.validate_python(value)


# --- ATR from candles --------------------------------------------------------------------

T0 = datetime(2026, 10, 24, tzinfo=UTC)
HOUR = timedelta(hours=1)


def bar(i: int, gap: int = 0) -> Candle:
    # Each candle ranges 99 to 101 and closes at 100: a true range of 2, every time.
    return Candle(
        ts=T0 + (i + gap) * HOUR,
        open=D(100),
        high=D(101),
        low=D(99),
        close=D(100),
        volume=D(1),
    )


def test_atr_needs_six_periods_of_candles() -> None:
    # Wilder's smoothing starts from a seed that fades by 1 - 1/period per candle; after
    # 6 periods it's down to ~0.2%, the same rule as RSI's warm-up.
    assert lookback(AtrRisk(period=14)) == 85
    assert lookback(AtrRisk(period=2)) == 13
    for no_atr in (AllIn(), FixedAmount(amount=D(1)), PercentOfEquity(percent=D(1))):
        assert lookback(no_atr) == 0


def test_atr_for_uses_the_last_lookback_candles() -> None:
    sizing = AtrRisk(period=2)
    assert atr_for(sizing, [bar(i) for i in range(13)], "1h") == D(2)
    # More history is fine: only the last 13 count.
    assert atr_for(sizing, [bar(i) for i in range(40)], "1h") == D(2)


def test_atr_for_refuses_too_few_candles_or_a_gap() -> None:
    sizing = AtrRisk(period=2)
    assert atr_for(sizing, [bar(i) for i in range(12)], "1h") is None
    with_gap = [bar(i) for i in range(6)] + [bar(i, gap=1) for i in range(6, 13)]
    assert atr_for(sizing, with_gap, "1h") is None


def test_atr_for_is_none_for_modes_without_atr() -> None:
    assert atr_for(AllIn(), [bar(i) for i in range(13)], "1h") is None


# --- The all-in building block -----------------------------------------------------------


def test_buy_amount_leaves_room_for_the_fee_and_rounds_down() -> None:
    # 1000 / (100 x 1.001) = 9.99000999..., down to the 0.0001 step.
    assert buy_amount(D(1000), D(100), D("0.001"), D(1), D("0.0001")) == D("9.9900")
    # 1000 / 300 = 3.333..., down (never up) to the 0.01 step.
    assert buy_amount(D(1000), D(300), D(0), D(1), D("0.01")) == D("3.33")
    assert buy_amount(D(1000), D(100), D(0), D("0.5"), D("0.0001")) == D(5)
