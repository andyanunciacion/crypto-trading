"""Stops: where they sit, when they fire, how a trailing stop follows the price (D40).

Entry at 100 unless a test says otherwise. Candles are (open, high, low, close); every
number is worked out by hand beside it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.risk.stops import (
    STOPS,
    AtrMultiple,
    Exit,
    ExitState,
    NoLevels,
    Percent,
    RMultiple,
    Stops,
    after_candle,
    after_price,
    exit_at_price,
    exit_on_candle,
    levels_at_entry,
    lookback,
    reentry,
)

D = Decimal
T0 = datetime(2026, 10, 24, tzinfo=UTC)
ENTRY = D(100)


def bar(open_: str, high: str, low: str, close: str, i: int = 0) -> Candle:
    return Candle(
        ts=T0 + i * timedelta(hours=1),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        volume=D(1),
    )


def levels(stops: Stops, entry: Decimal = ENTRY, atr: str | None = None) -> ExitState:
    state = levels_at_entry(stops, entry, None if atr is None else D(atr))
    assert isinstance(state, ExitState), state
    return state


def pct(value: str) -> Percent:
    return Percent(percent=D(value))


SL5_TP10 = Stops(stop_loss=pct("5"), take_profit=pct("10"))


# --- Where the levels sit -----------------------------------------------------------------


def test_a_percent_stop_loss_sits_below_the_entry() -> None:
    # 100 x (1 - 5%) = 95.
    state = levels(Stops(stop_loss=pct("5")))
    assert (state.stop, state.take_profit, state.peak) == (D(95), None, ENTRY)


def test_an_atr_stop_loss_sits_multiples_of_atr_below() -> None:
    # 100 - 2 x ATR 5 = 90.
    assert levels(Stops(stop_loss=AtrMultiple(multiple=D(2))), atr="5").stop == D(90)


def test_a_percent_take_profit_sits_above_the_entry() -> None:
    # 100 x (1 + 10%) = 110.
    assert levels(Stops(take_profit=pct("10"))).take_profit == D(110)


def test_an_r_take_profit_is_a_multiple_of_what_is_risked() -> None:
    # The stop risks 100 - 95 = 5 a coin; 2R is 2 x 5 = 10 above the entry: 110.
    stops = Stops(stop_loss=pct("5"), take_profit=RMultiple(multiple=D(2)))
    assert levels(stops).take_profit == D(110)


def test_a_trailing_stop_starts_below_the_entry() -> None:
    # Alone, it's also the first stop: 100 x (1 - 10%) = 90.
    assert levels(Stops(trailing=pct("10"))).stop == D(90)


def test_the_higher_of_stop_loss_and_trailing_is_the_stop() -> None:
    # 5% stop at 95, 3% trail at 97: the trail is closer, so it's the stop.
    assert levels(Stops(stop_loss=pct("5"), trailing=pct("3"))).stop == D(97)
    assert levels(Stops(stop_loss=pct("5"), trailing=pct("10"))).stop == D(95)


def test_an_atr_stop_needs_an_atr() -> None:
    atr_stop = Stops(stop_loss=AtrMultiple(multiple=D(2)))
    no_atr = levels_at_entry(atr_stop, ENTRY, None)
    assert isinstance(no_atr, NoLevels) and "no ATR" in no_atr.reason
    flat = levels_at_entry(atr_stop, ENTRY, D(0))
    assert isinstance(flat, NoLevels) and "ATR is 0" in flat.reason
    # Percent stops need none.
    assert levels(Stops(stop_loss=pct("5"))).stop == D(95)


def test_no_stops_means_no_levels_to_hit() -> None:
    state = levels(Stops())
    assert (state.stop, state.take_profit) == (None, None)
    assert exit_on_candle(state, bar("100", "200", "1", "100")) is None


# --- When they fire -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candle", "expected"),
    [
        (bar("100", "101", "96", "98"), None),  # between the two: nothing
        (bar("100", "101", "95", "97"), Exit("stop_loss", D(95))),  # touched: at the stop
        (bar("93", "94", "90", "92"), Exit("stop_loss", D(93))),  # gapped through: the open
        (bar("100", "110", "99", "105"), Exit("take_profit", D(110))),  # touched
        (bar("112", "115", "111", "113"), Exit("take_profit", D(112))),  # gapped above
        # Both inside one candle: which came first can't be told, so the worse one.
        (bar("100", "111", "94", "105"), Exit("stop_loss", D(95))),
    ],
    ids=["neither", "stop", "gap-down", "take-profit", "gap-up", "both"],
)
def test_exits_on_known_candles(candle: Candle, expected: Exit | None) -> None:
    assert exit_on_candle(levels(SL5_TP10), candle) == expected


def test_a_trailing_stop_follows_the_highs_and_never_falls() -> None:
    trailing = Stops(trailing=pct("10"))
    state = levels(trailing)  # stop 90
    # Highs 110 then 120: stops 99 then 108. A lower high (115) leaves it at 108.
    for high, stop in (("110", "99"), ("120", "108"), ("115", "108")):
        state = after_candle(state, trailing, bar("105", high, "104", "105"))
        assert state.stop == D(stop)
    assert state.peak == D(120)

    assert exit_on_candle(state, bar("110", "111", "107", "108")) == Exit("trailing_stop", D(108))


def test_a_candles_own_high_doesnt_lift_the_stop_its_low_hits() -> None:
    trailing = Stops(trailing=pct("10"))
    state = after_candle(levels(trailing), trailing, bar("110", "120", "110", "118"))  # 108
    # High 130 would lift the stop to 117, and the low of 115 is below that. But the high
    # may have come after the low, so this candle is checked against 108: no exit.
    big = bar("125", "130", "115", "120")
    assert exit_on_candle(state, big) is None
    state = after_candle(state, trailing, big)
    assert state.stop == D(117)
    # The next candle's low of 116 does reach it.
    assert exit_on_candle(state, bar("118", "119", "116", "117")) == Exit("trailing_stop", D(117))


def test_a_trailing_stop_past_the_stop_loss_fires_as_trailing() -> None:
    stops = Stops(stop_loss=pct("5"), trailing=pct("10"))
    state = levels(stops)  # 95, the stop-loss
    assert exit_on_candle(state, bar("96", "97", "94", "95")) == Exit("stop_loss", D(95))
    state = after_candle(state, stops, bar("110", "120", "110", "118"))  # trail 108
    assert exit_on_candle(state, bar("110", "111", "107", "108")) == Exit("trailing_stop", D(108))


def test_an_atr_trailing_stop_keeps_the_entry_atr() -> None:
    # 3 x ATR 2 = 6 below the peak: 94 at entry, 104 after a high of 110.
    stops = Stops(trailing=AtrMultiple(multiple=D(3)))
    state = levels(stops, atr="2")
    assert state.stop == D(94)
    assert after_candle(state, stops, bar("105", "110", "104", "108")).stop == D(104)


# --- Live prices (paper and live bots) ----------------------------------------------------


@pytest.mark.parametrize(
    ("bid", "expected"),
    [
        ("96", None),  # between the two: nothing
        ("95", Exit("stop_loss", D(95))),  # at the stop
        ("93", Exit("stop_loss", D(93))),  # past it: the bid is the price
        ("110", Exit("take_profit", D(110))),  # at the take-profit
        ("112", Exit("take_profit", D(112))),
    ],
    ids=["neither", "stop", "below-stop", "take-profit", "above-take-profit"],
)
def test_exits_on_known_bids(bid: str, expected: Exit | None) -> None:
    assert exit_at_price(levels(SL5_TP10), D(bid)) == expected


def test_no_stops_means_no_bid_hits_a_level() -> None:
    state = levels(Stops())
    assert exit_at_price(state, D("0.01")) is None
    assert exit_at_price(state, D(1_000_000)) is None


def test_a_trailing_stop_follows_the_bid_and_never_falls() -> None:
    trailing = Stops(trailing=pct("10"))
    state = levels(trailing)  # stop 90
    # Bids 110 then 120: stops 99 then 108. A lower bid (115) leaves it at 108.
    for bid, stop in (("110", "99"), ("120", "108"), ("115", "108")):
        state = after_price(state, trailing, D(bid))
        assert state.stop == D(stop)
    assert state.peak == D(120)
    assert exit_at_price(state, D("108.5")) is None
    assert exit_at_price(state, D(108)) == Exit("trailing_stop", D(108))


def test_a_trailing_stop_past_the_stop_loss_fires_as_trailing_on_a_bid() -> None:
    stops = Stops(stop_loss=pct("5"), trailing=pct("10"))
    state = levels(stops)  # 95, the stop-loss
    assert exit_at_price(state, D(95)) == Exit("stop_loss", D(95))
    state = after_price(state, stops, D(120))  # trail 108
    assert exit_at_price(state, D(107)) == Exit("trailing_stop", D(107))


def test_a_new_average_entry_places_the_levels_again() -> None:
    # A buy filled in slices: first at 100, then the average became 102.
    stops = Stops(stop_loss=pct("5"), take_profit=RMultiple(multiple=D(2)))
    moved = reentry(levels(stops), stops, D(102))
    # 102 x 0.95 = 96.9; R = 5.1, so 2R above 102 = 112.2.
    assert isinstance(moved, ExitState)
    assert (moved.stop, moved.stop_loss, moved.take_profit, moved.peak) == (
        D("96.9"),
        D("96.9"),
        D("112.2"),
        D(102),
    )


def test_a_new_average_entry_keeps_the_trailing_peak() -> None:
    trailing = Stops(trailing=pct("10"))
    state = after_price(levels(trailing), trailing, D(110))  # peak 110, stop 99
    moved = reentry(state, trailing, D(101))
    # Still 10% below the 110 peak, not below the new 101 entry.
    assert isinstance(moved, ExitState) and (moved.peak, moved.stop) == (D(110), D(99))


def test_a_new_average_entry_keeps_the_entry_atr() -> None:
    stops = Stops(stop_loss=AtrMultiple(multiple=D(2)))
    moved = reentry(levels(stops, atr="5"), stops, D(104))
    # 104 - 2 x 5 = 94.
    assert isinstance(moved, ExitState) and (moved.stop, moved.atr) == (D(94), D(5))


# --- Settings -----------------------------------------------------------------------------


def test_atr_modes_need_six_periods_of_candles() -> None:
    assert lookback(Stops()) == 0
    assert lookback(Stops(stop_loss=pct("5"), trailing=pct("3"))) == 0
    assert lookback(Stops(trailing=AtrMultiple(multiple=D(3)))) == 85  # 6 x 14 + 1
    assert lookback(Stops(stop_loss=AtrMultiple(multiple=D(2)), atr_period=5)) == 31


@pytest.mark.parametrize(
    "stops",
    [
        Stops(),
        SL5_TP10,
        Stops(
            stop_loss=AtrMultiple(multiple=D(2)),
            take_profit=RMultiple(multiple=D("1.5")),
            trailing=pct("3"),
            atr_period=20,
        ),
    ],
)
def test_stops_round_trip_through_json(stops: Stops) -> None:
    assert STOPS.validate_json(STOPS.dump_json(stops)) == stops


def test_the_kind_picks_the_settings() -> None:
    parsed = STOPS.validate_python(
        {"stop_loss": {"kind": "atr", "multiple": "2"}, "take_profit": {"kind": "r", "multiple": 2}}
    )
    assert parsed == Stops(
        stop_loss=AtrMultiple(multiple=D(2)), take_profit=RMultiple(multiple=D(2))
    )


@pytest.mark.parametrize(
    "value",
    [
        {"take_profit": {"kind": "r", "multiple": 2}},  # R of what? no stop-loss
        {"stop_loss": {"kind": "percent", "percent": 0}},
        {"stop_loss": {"kind": "percent", "percent": -5}},
        {"stop_loss": {"kind": "percent", "percent": 100}},
        {"stop_loss": {"kind": "atr", "multiple": 0}},
        {"stop_loss": {"kind": "r", "multiple": 2}},  # R is for take-profits only
        {"trailing": {"kind": "magic"}},
        {"stop_loss": {"kind": "atr", "multiple": 2}, "atr_period": 0},
        {"stoploss": {"kind": "percent", "percent": 5}},  # a typo, not silently ignored
    ],
)
def test_bad_settings_are_refused(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        STOPS.validate_python(value)
