"""What a live price does to a position's stops (bots/exits.py, D40), with no database.

Entry at 100, a 5% stop-loss (95) and a 10% take-profit (110) unless a test says
otherwise. The bot runner's tests drive the same rules through a real PaperBroker.
"""

from decimal import Decimal

import pytest

from engine.bots.exits import (
    ExitOrder,
    Hold,
    Sell,
    StopPlan,
    Trail,
    Wait,
    exit_order_id,
    on_bid,
)
from engine.risk.stops import (
    AtrMultiple,
    ExitKind,
    ExitState,
    Percent,
    RMultiple,
    Stops,
    levels_at_entry,
)

D = Decimal
SL5_TP10 = Stops(stop_loss=Percent(percent=D(5)), take_profit=Percent(percent=D(10)))
TRAIL10 = Stops(trailing=Percent(percent=D(10)))


def levels(stops: Stops = SL5_TP10) -> ExitState:
    state = levels_at_entry(stops, D(100), None)
    assert isinstance(state, ExitState)
    return state


def exit_(status: str, limit: str, kind: ExitKind = "stop_loss") -> ExitOrder:
    return ExitOrder(client_order_id="b1-202610221230-x1", status=status, limit=D(limit), kind=kind)


@pytest.mark.parametrize(
    ("bid", "expected"),
    [
        ("96", Hold()),  # between the two, and nothing trails
        ("95", Sell("stop_loss", again=False)),
        ("94", Sell("stop_loss", again=False)),
        ("110", Sell("take_profit", again=False)),
    ],
    ids=["neither", "stop", "below-stop", "take-profit"],
)
def test_a_bid_at_a_level_sells(bid: str, expected: Hold | Sell) -> None:
    assert on_bid(levels(), SL5_TP10, D(bid), []) == expected


def test_a_new_high_lifts_a_trailing_stop() -> None:
    state = levels(TRAIL10)  # stop 90
    action = on_bid(state, TRAIL10, D(120), [])
    assert isinstance(action, Trail) and (action.levels.stop, action.levels.peak) == (
        D(108),
        D(120),
    )
    # Not a new high: nothing to store.
    assert on_bid(action.levels, TRAIL10, D(115), []) == Hold()
    assert on_bid(action.levels, TRAIL10, D(108), []) == Sell("trailing_stop", again=False)


def test_without_a_trailing_stop_a_new_high_changes_nothing() -> None:
    assert on_bid(levels(), SL5_TP10, D(105), []) == Hold()


def test_an_exit_that_still_reaches_the_bid_is_left_to_fill() -> None:
    # Limit 94.95: a bid at or above it still fills it, so no second exit.
    taking = [exit_("open", "94.95")]
    assert on_bid(levels(), SL5_TP10, D(95), taking) == Wait()
    assert on_bid(levels(), SL5_TP10, D("94.95"), taking) == Wait()


def test_an_exit_the_bid_fell_below_is_placed_again() -> None:
    assert on_bid(levels(), SL5_TP10, D(94), [exit_("open", "94.95")]) == Sell(
        "stop_loss", again=True
    )


def test_once_a_stop_fired_the_position_is_sold_whatever_the_price() -> None:
    # The first exit was cancelled with something left (at a candle close, say). Even
    # with the bid back above the stop, what's left is sold, as by the same exit.
    cancelled = [exit_("canceled", "94.95", kind="trailing_stop")]
    assert on_bid(levels(), SL5_TP10, D(99), cancelled) == Sell("trailing_stop", again=True)


def test_exit_ids_follow_the_buy_and_count_up() -> None:
    assert exit_order_id("b4-202610241429-b", 1) == "b4-202610241429-x1"
    assert exit_order_id("b4-202610241429-b", 12) == "b4-202610241429-x12"
    with pytest.raises(ValueError, match="isn't a buy"):
        exit_order_id("b4-202610241429-s", 1)
    with pytest.raises(ValueError, match="at least 1"):
        exit_order_id("b4-202610241429-b", 0)


@pytest.mark.parametrize(
    "plan",
    [
        StopPlan(SL5_TP10, None),
        StopPlan(
            Stops(
                stop_loss=AtrMultiple(multiple=D(2)),
                take_profit=RMultiple(multiple=D(2)),
                atr_period=3,
            ),
            D("5.25"),
        ),
    ],
)
def test_a_stop_plan_round_trips_through_an_orders_reason(plan: StopPlan) -> None:
    assert StopPlan.from_reason({"stops": plan.to_json()}) == plan


def test_a_buy_without_stops_has_no_plan() -> None:
    assert StopPlan.from_reason({"signal": {}}) is None
