"""Grid trading on hand-built closes, where the lines each close crossed are known (P6-6).

Unless a test says otherwise: lower 90, upper 110, 4 grids, so the lines are every 5:

    line   90   95   100   105   110
    i       0    1     2     3     4

Slice i is bought when a close falls below line i (i < 4) and sold when a close reaches
line i + 1. So the grid holds the top k slices, k between what the lines above the close
force (`k_down`, the buy lines above it) and allow (`k_up`, the sell lines above it):

    close   >= 110  105-110  100-105  95-100  90-95  < 90
    k_down       0        0        1       2      3     4
    k_up         0        1        2       3      4     4
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.strategies.base import Position, Signal
from engine.strategies.grid import Grid, GridParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
D = Decimal
GRID = Grid(GridParams(lower=D(90), upper=D(110), grids=4))
HOLDING = Position(amount=D(1), avg_entry=D(100))


def candles(closes: list[str]) -> list[Candle]:
    def one(i: int, close: str) -> Candle:
        price = D(close)
        return Candle(ts=T0 + i * HOUR, open=price, high=price, low=price, close=price, volume=D(1))

    return [one(i, close) for i, close in enumerate(closes)]


def decide(closes: list[str], position: Position | None = HOLDING, grid: Grid = GRID) -> Signal:
    """The signal for the last of `closes`, padded in front with the first one to a whole
    window (a flat start crosses no line)."""
    padded = [closes[0]] * (grid.warmup_candles - len(closes)) + closes
    return grid.on_candle(candles(padded), position)


def test_a_close_below_a_line_buys_a_slice() -> None:
    # 107 -> 102: below line 105, so the grid holds 1 slice: a quarter of the budget.
    signal = decide(["107", "102"], position=None)
    assert (signal.action, signal.size_hint) == ("buy", D("0.25"))
    assert signal.indicators == {"close": 102.0, "k_prev": 0.0, "k": 1.0}


def test_a_close_through_several_lines_buys_a_slice_for_each() -> None:
    # 107 -> 97: below 105 and 100, two slices.
    assert decide(["107", "97"], position=None).size_hint == D("0.5")
    # 107 -> 88: below every buy line, the whole budget.
    assert decide(["107", "88"], position=None).size_hint == D(1)


def test_a_slice_is_sold_one_line_above_where_it_was_bought() -> None:
    # Bought below 105 (at 102); back up through 105 to 107 sells nothing: that slice's
    # sell line is 110, one grid step of profit above.
    assert decide(["107", "102", "107"]).action == "hold"
    # Bought 2 slices at 97 (lines 105 and 100). Up to 102 sells nothing; up to 106 reaches
    # line 105, the sell line of the slice bought at 100: half of the 2 held.
    assert decide(["107", "97", "102"]).action == "hold"
    signal = decide(["107", "97", "102", "106"])
    assert (signal.action, signal.size_hint) == ("sell", D("0.5"))


def test_a_close_at_a_line_reaches_it() -> None:
    # Exactly 105 sells the slice bought below 100; exactly 100 doesn't buy below 100.
    assert decide(["107", "97", "105"]).action == "sell"
    assert decide(["107", "102", "100"]).action == "hold"


def test_selling_down_to_nothing_sells_everything() -> None:
    signal = decide(["107", "102", "110"])
    assert (signal.action, signal.size_hint) == ("sell", None)


def test_at_or_above_the_top_a_held_position_is_sold_whatever_the_grid_held() -> None:
    # Exits are states (D31): a bot that holds above the range (say it switched to the grid
    # with a position) sells it, though no line was crossed.
    signal = decide(["112", "115"])
    assert (signal.action, signal.size_hint) == ("sell", None)
    assert decide(["112", "115"], position=None).action == "hold"


def test_buying_back_after_a_sell_adds_the_slices_crossed() -> None:
    # Holds 2 (bought at 97), sells 1 at 106, then 99 buys back the one below 100.
    signal = decide(["107", "97", "106", "99"])
    assert (signal.action, signal.size_hint) == ("buy", D("0.25"))


def test_a_flat_grid_buys_everything_the_grid_holds_at_its_next_crossing() -> None:
    # The grid holds 2 slices from 97 on, but the bot holds nothing (it just started, or a
    # stop sold everything): at the next line it buys all 3 slices the grid holds then.
    signal = decide(["107", "97", "97", "92"], position=None)
    assert (signal.action, signal.size_hint) == ("buy", D("0.75"))
    # No line crossed: a flat grid waits rather than buying on every candle.
    assert decide(["107", "97", "97"], position=None).action == "hold"


def test_a_flat_grid_has_nothing_to_sell() -> None:
    signal = decide(["107", "97", "106"], position=None)
    assert (signal.action, signal.reason) == ("hold", "close reached a sell line; nothing to sell")


def test_below_the_range_it_holds_everything() -> None:
    assert decide(["107", "88", "80", "70"]).action == "hold"


def test_what_came_before_the_first_crossing_doesnt_matter() -> None:
    # The window starts in the 100-105 band, which leaves one thing unknown: was the slice
    # bought below 100 still held? The first crossing settles it, so from there on the
    # signals are the same, whether the window began above the band or in it.
    for start in ("107", "103", "101"):
        signal = decide([start, "102", "97", "101", "106"])
        assert (signal.action, signal.size_hint) == ("sell", D("0.5")), start


def test_the_window_is_two_hundred_candles() -> None:
    assert GRID.warmup_candles == 200
    assert Grid.scales_in


@pytest.mark.parametrize(
    "params",
    [
        {"lower": "100", "upper": "100"},
        {"lower": "110", "upper": "100"},
        {"lower": "0", "upper": "100"},
        {"lower": "90", "upper": "110", "grids": 1},
        {"lower": "90", "upper": "110", "grids": 101},
        {"upper": "110"},
    ],
)
def test_bad_params_are_refused(params: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GridParams.model_validate(params)


def test_uneven_lines_divide_exactly_enough() -> None:
    # 3 grids from 100 to 200: lines at 133.33..., 166.66...; Decimal, not float.
    grid = Grid(GridParams(lower=D(100), upper=D(200), grids=3))
    assert grid.lines()[1] == D(100) + D(100) / 3
    signal = decide(["190", "150"], position=None, grid=grid)
    assert (signal.action, signal.size_hint) == ("buy", D(1) / 3)
