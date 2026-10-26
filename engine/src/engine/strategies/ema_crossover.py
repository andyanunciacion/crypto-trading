"""EMA crossover: for trending markets.

Buy when the fast EMA crosses above the slow one (a "golden cross"), sell when it
crosses back below (a "death cross"). Crosses are events, so a bot that starts in the
middle of a trend waits for the next cross instead of buying late.
"""

from collections.abc import Sequence
from typing import Self

from pydantic import Field, model_validator

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position, Signal, Strategy, StrategyParams
from shared.indicators import ema


class EmaCrossoverParams(StrategyParams):
    fast: int = Field(default=9, ge=2, description="Fast EMA period, in candles")
    slow: int = Field(default=21, ge=3, description="Slow EMA period, in candles")

    @model_validator(mode="after")
    def _fast_below_slow(self) -> Self:
        if self.fast >= self.slow:
            raise ValueError(f"fast ({self.fast}) must be shorter than slow ({self.slow})")
        return self


class EmaCrossover(Strategy[EmaCrossoverParams]):
    name = "ema_crossover"
    version = 1
    params_model = EmaCrossoverParams

    @property
    def warmup_candles(self) -> int:
        # An EMA never fully forgets its start-up seed, but the seed's weight shrinks by
        # (1 - alpha) per candle: after 3 x slow candles it's about 0.2% of the value.
        return 3 * self.params.slow + 1

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        closes = [float(candle.close) for candle in window]
        fast = ema(closes, self.params.fast)
        slow = ema(closes, self.params.slow)
        indicators = {
            "prev_fast_ema": fast[-2],
            "prev_slow_ema": slow[-2],
            "fast_ema": fast[-1],
            "slow_ema": slow[-1],
        }
        ts = window[-1].ts

        def signal(action: Action, reason: str) -> Signal:
            return Signal(action=action, ts=ts, reason=reason, indicators=indicators)

        if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
            if position is None:
                return signal("buy", "fast EMA crossed above slow EMA")
            return signal("hold", "fast EMA crossed above slow EMA; already holding")
        if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
            if position is not None:
                return signal("sell", "fast EMA crossed below slow EMA")
            return signal("hold", "fast EMA crossed below slow EMA; nothing to sell")
        return signal("hold", "no crossover")
