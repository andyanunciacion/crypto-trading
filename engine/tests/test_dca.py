"""DCA (dollar-cost averaging) on hand-built hourly candles (P6-6).

Unless a test says otherwise: a buy every 24 candles, an extra buy when the close falls
5% below the last buy, and everything sold 10% above the average entry. Candle i opens
at 2026-01-01 00:00 UTC + i hours, so the 24-hourly schedule lands on i = 0, 24, 48...
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.strategies.base import Position, Signal
from engine.strategies.dca import Dca, DcaParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
D = Decimal
DCA = Dca(DcaParams(interval=24, dip_percent=D(5), take_profit_percent=D(10)))
AT_100 = Position(amount=D(1), avg_entry=D(100), last_entry=D(100))
# Bought 1 at 100, then 1 more at 95 on a dip: an average of 97.5, the last buy at 95.
LADDERED = Position(amount=D(2), avg_entry=D("97.5"), last_entry=D(95))


def decide(
    last: int,
    closes: tuple[str, str] = ("100", "100"),
    position: Position | None = None,
    dca: Dca = DCA,
) -> Signal:
    """The signal for candle `last`, after candle `last - 1`; their closes are `closes`."""

    def one(i: int, close: str) -> Candle:
        price = D(close)
        return Candle(ts=T0 + i * HOUR, open=price, high=price, low=price, close=price, volume=D(1))

    return dca.on_candle([one(last - 1, closes[0]), one(last, closes[1])], position)


def test_it_buys_on_the_schedule() -> None:
    assert decide(24).action == "buy"
    assert decide(48).action == "buy"
    assert [decide(i).action for i in (23, 25, 47)] == ["hold"] * 3
    assert decide(24).reason == "every 24 candles"
    assert decide(24).size_hint is None  # the bot's sizing, as it is


def test_the_schedule_follows_the_clock_not_when_the_bot_started() -> None:
    # Candle 24 opens at 00:00 UTC: a bot started at any hour buys then, as a backtest does.
    assert decide(24).ts.hour == 0
    every_6 = Dca(DcaParams(interval=6))
    assert [i for i in range(1, 25) if decide(i, dca=every_6).action == "buy"] == [6, 12, 18, 24]


def test_it_keeps_buying_on_the_schedule_while_holding() -> None:
    assert decide(24, position=AT_100).action == "buy"


def test_a_close_falling_through_the_dip_level_buys_more() -> None:
    # 5% below the last buy at 100: 95. 96 -> 94 falls through it.
    signal = decide(5, ("96", "94"), AT_100)
    assert (signal.action, signal.reason) == ("buy", "close fell 5% below the last buy")
    assert signal.indicators == {
        "close": 94.0,
        "avg_entry": 100.0,
        "last_entry": 100.0,
        "dip_level": 95.0,
        "take_profit_level": 110.0,
    }


def test_staying_below_the_dip_level_buys_once() -> None:
    # An event, not a state: a crash that stays down isn't bought on every candle.
    assert decide(5, ("94", "93"), AT_100).action == "hold"
    assert decide(5, ("95", "94"), AT_100).action == "buy"  # 95 isn't below 95 yet


def test_the_next_dip_is_measured_from_the_last_buy_not_the_average() -> None:
    # After buying at 95, the next dip level is 95 x 0.95 = 90.25. Measured from the 97.5
    # average it would be 92.625, which a wobble from 93 to 92 crosses: version 1 bought
    # again on every such wobble (D58).
    assert decide(5, ("93", "92"), LADDERED).action == "hold"
    signal = decide(5, ("91", "90"), LADDERED)
    assert (signal.action, signal.indicators["dip_level"]) == ("buy", 90.25)


def test_without_a_last_buy_the_dip_is_measured_from_the_average() -> None:
    # A position no buy of the bot made (by hand) has no last buy to go by.
    by_hand = Position(amount=D(1), avg_entry=D(100))
    signal = decide(5, ("96", "94"), by_hand)
    assert (signal.action, signal.indicators["dip_level"]) == ("buy", 95.0)
    assert "last_entry" not in signal.indicators


def test_a_dip_needs_a_position() -> None:
    assert decide(5, ("96", "94")).action == "hold"


def test_the_take_profit_sells_everything() -> None:
    signal = decide(5, ("100", "110"), AT_100)
    assert (signal.action, signal.size_hint) == ("sell", None)
    assert signal.reason == "close is 10% above the average entry"
    # A state: still above it on the next candle, still a SELL.
    assert decide(6, ("111", "112"), AT_100).action == "sell"
    assert decide(5, ("100", "109.99"), AT_100).action == "hold"


def test_the_take_profit_comes_before_a_scheduled_buy() -> None:
    assert decide(24, ("100", "110"), AT_100).action == "sell"


def test_a_dip_on_the_schedule_is_one_buy() -> None:
    signal = decide(24, ("96", "94"), AT_100)
    assert (signal.action, signal.reason) == ("buy", "close fell 5% below the last buy")


def test_zero_turns_the_dip_and_the_take_profit_off() -> None:
    plain = Dca(DcaParams(interval=24))
    assert decide(5, ("96", "94"), AT_100, plain).action == "hold"
    assert decide(5, ("100", "500"), AT_100, plain).action == "hold"  # accumulates forever
    assert decide(5, ("100", "500"), AT_100, plain).indicators == {
        "close": 500.0,
        "avg_entry": 100.0,
        "last_entry": 100.0,
    }


def test_it_needs_only_the_last_two_candles() -> None:
    assert DCA.warmup_candles == 2
    assert Dca.scales_in
    assert Dca.version == 2  # version 1 measured the dip from the average entry


@pytest.mark.parametrize(
    "params",
    [{"interval": 0}, {"dip_percent": "-1"}, {"dip_percent": "100"}, {"take_profit_percent": "-1"}],
)
def test_bad_params_are_refused(params: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DcaParams.model_validate(params)
