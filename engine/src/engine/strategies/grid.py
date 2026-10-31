"""Grid trading: for sideways markets.

P6-6, D58. Evenly spaced price lines split a range into `grids` steps. Each time the price falls
below a line it buys a slice, and each slice is sold one line higher, so every round
trip banks one grid step. In a range that keeps swinging, that's many small gains; if
the price leaves the range downwards, it's left holding every slice (there's no stop of
its own: give the bot one). At or above the top it sells everything.

It decides on closes, like every strategy here: a close past a line counts, a wick that
touches one and closes back doesn't, and the order fills at the next open (D29).

Slices are fractions of the bot's sizing, which here is the whole grid's budget: each
BUY's `size_hint` is the slices it buys / `grids`. With `fixed` sizing every slice costs
the same; with `all_in`, each is a share of the cash still free, so later slices are
smaller. Each SELL's `size_hint` is the share of the slices held that it sells, so a buy
the risk layer skipped or shrank makes the later sells smaller, never wrong.

Stateless (D28): the grid always holds its top k slices (bought at the highest lines),
and k depends only on the closes. A close forces at least the slices whose buy line is
above it (`k_down`) and allows at most those whose sell line is above it (`k_up`), one
more inside the range; between the two, k stays as it was. So the only unknown is
whether that one slice is held, and the first line the window's closes cross settles
it. Replaying the window from its first candle gives k at its last two closes.

A flat bot holds nothing whatever the grid says: it just started, or a stop sold
everything. At the next line crossed it buys every slice the grid holds then, rather
than on every candle.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position, Signal, Strategy, StrategyParams

# Candles replayed to work out what the grid holds. Its one unknown is settled at the first
# line crossed, so this only has to be longer than the price usually stays between lines.
WINDOW = 200


class GridParams(StrategyParams):
    lower: Decimal = Field(gt=0, description="The bottom line of the grid, in the quote currency")
    upper: Decimal = Field(
        gt=0, description="The top line: at or above it, everything held is sold"
    )
    grids: int = Field(
        default=10,
        ge=2,
        le=100,
        description=(
            "Steps between the bottom and top lines; each buys one slice of the bot's sizing"
            " (use fixed sizing for equal slices)"
        ),
    )

    @model_validator(mode="after")
    def _lower_below_upper(self) -> Self:
        if self.lower >= self.upper:
            raise ValueError(f"lower ({self.lower}) must be below upper ({self.upper})")
        return self


class Grid(Strategy[GridParams]):
    name = "grid"
    version = 1
    params_model = GridParams
    scales_in = True

    @property
    def warmup_candles(self) -> int:
        return WINDOW

    def lines(self) -> list[Decimal]:
        """The grids + 1 lines, bottom first."""
        lower, upper, grids = self.params.lower, self.params.upper, self.params.grids
        step = (upper - lower) / grids
        return [lower + i * step for i in range(grids)] + [upper]

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        lines = self.lines()
        buy_lines, sell_lines = lines[:-1], lines[1:]

        def held_after(k: int, close: Decimal) -> int:
            forced = sum(1 for line in buy_lines if line > close)
            allowed = sum(1 for line in sell_lines if line > close)
            return max(forced, min(k, allowed))

        closes = [candle.close for candle in window]
        k = sum(1 for line in buy_lines if line > closes[0])  # a guess the first crossing fixes
        for close in closes[1:-1]:
            k = held_after(k, close)
        k_prev, k_now = k, held_after(k, closes[-1])
        close = closes[-1]
        indicators = {"close": float(close), "k_prev": float(k_prev), "k": float(k_now)}
        ts = window[-1].ts

        def signal(action: Action, reason: str, size_hint: Decimal | None = None) -> Signal:
            return Signal(
                action=action, ts=ts, reason=reason, indicators=indicators, size_hint=size_hint
            )

        if close >= self.params.upper:
            if position is not None:
                return signal("sell", "close is at or above the grid's top line")
            return signal("hold", "close is at or above the grid's top line; nothing to sell")
        if k_now > k_prev:
            held = 0 if position is None else k_prev
            hint = Decimal(k_now - held) / self.params.grids
            return signal("buy", f"close fell below a buy line: the grid holds {k_now}", hint)
        if k_now < k_prev:
            if position is None:
                return signal("hold", "close reached a sell line; nothing to sell")
            reason = f"close reached a sell line: the grid holds {k_now} of {k_prev}"
            if k_now == 0:
                return signal("sell", reason)
            return signal("sell", reason, Decimal(k_prev - k_now) / k_prev)
        return signal("hold", f"no line crossed: the grid holds {k_now}")
