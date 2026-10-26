"""RSI mean reversion: for ranging markets.

RSI measures recent gains against recent losses on a 0-100 scale; a low RSI means the
price has been pushed down hard ("oversold"), and in a range it tends to snap back.
Buy when RSI turns back up through the oversold level, rather than while it's still
falling, so a crash that stays oversold isn't bought all the way down. Sell whenever
RSI is overbought: a state, not a cross, so a bot that missed the crossing candle
still gets out (D31).
"""

from collections.abc import Sequence
from typing import Self

from pydantic import Field, model_validator

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position, Signal, Strategy, StrategyParams
from shared.indicators import rsi


class RsiMeanReversionParams(StrategyParams):
    period: int = Field(default=14, ge=2, description="RSI period, in candles")
    oversold: float = Field(
        default=30, gt=0, lt=100, description="Buy when RSI rises back above this level"
    )
    overbought: float = Field(
        default=70, gt=0, lt=100, description="Sell when RSI is at or above this level"
    )

    @model_validator(mode="after")
    def _oversold_below_overbought(self) -> Self:
        if self.oversold >= self.overbought:
            raise ValueError(
                f"oversold ({self.oversold}) must be below overbought ({self.overbought})"
            )
        return self


class RsiMeanReversion(Strategy[RsiMeanReversionParams]):
    name = "rsi_mean_reversion"
    version = 1
    params_model = RsiMeanReversionParams

    @property
    def warmup_candles(self) -> int:
        # Wilder's smoothing never fully forgets its seed either; its weight shrinks by
        # (1 - 1/period) per candle, to about 0.25% after 6 x period. Plus one candle,
        # because RSI works on the changes between closes.
        return 6 * self.params.period + 1

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        values = rsi([float(candle.close) for candle in window], self.params.period)
        indicators = {"prev_rsi": values[-2], "rsi": values[-1]}
        ts = window[-1].ts

        def signal(action: Action, reason: str) -> Signal:
            return Signal(action=action, ts=ts, reason=reason, indicators=indicators)

        oversold, overbought = self.params.oversold, self.params.overbought
        if values[-2] < oversold <= values[-1]:
            if position is None:
                return signal("buy", "RSI rose back above oversold")
            return signal("hold", "RSI rose back above oversold; already holding")
        if values[-1] >= overbought:
            if position is not None:
                return signal("sell", "RSI is overbought")
            return signal("hold", "RSI is overbought; nothing to sell")
        return signal("hold", "RSI between its levels")
