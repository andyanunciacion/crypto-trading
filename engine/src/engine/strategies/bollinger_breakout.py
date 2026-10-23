"""Bollinger breakout: for volatility expansions.

Bollinger Bands sit a few standard deviations either side of a moving average, so they
widen and narrow with volatility. A close that breaks above the upper band on heavy
volume suggests a real move with buyers behind it. Buy on that break (an event, so a
bot that starts mid-move doesn't chase it); sell whenever the close is back below the
middle band, the sign the move has run out (a state, so a bot that missed the crossing
candle still gets out, D31).
"""

from collections.abc import Sequence

from pydantic import Field

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position, Signal, Strategy, StrategyParams
from engine.strategies.indicators import bollinger


class BollingerBreakoutParams(StrategyParams):
    period: int = Field(default=20, ge=2, description="Band period, in candles")
    width: float = Field(
        default=2.0, gt=0, description="Band width, in standard deviations from the middle"
    )
    volume_factor: float = Field(
        default=1.5,
        ge=0,
        description=(
            "A breakout counts only if its volume is at least this many times the average "
            "of the period before it; 0 turns the volume filter off"
        ),
    )


class BollingerBreakout(Strategy[BollingerBreakoutParams]):
    name = "bollinger_breakout"
    version = 1
    params_model = BollingerBreakoutParams

    @property
    def warmup_candles(self) -> int:
        # The previous candle's band, and the period of volume before the current candle.
        # A simple average has no start-up seed to fade, unlike an EMA.
        return self.params.period + 1

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        period = self.params.period
        closes = [float(candle.close) for candle in window]
        prev_bands, bands = bollinger(closes, period, self.params.width)
        volume = float(window[-1].volume)
        # The candles before this one, so a spike doesn't raise its own baseline.
        avg_volume = sum(float(candle.volume) for candle in window[:-1]) / period
        indicators = {
            "prev_close": closes[-2],
            "prev_upper": prev_bands.upper,
            "close": closes[-1],
            "upper": bands.upper,
            "middle": bands.middle,
            "lower": bands.lower,
            "volume": volume,
            "avg_volume": avg_volume,
        }
        ts = window[-1].ts

        def signal(action: Action, reason: str) -> Signal:
            return Signal(action=action, ts=ts, reason=reason, indicators=indicators)

        if closes[-2] <= prev_bands.upper and closes[-1] > bands.upper:
            if volume < self.params.volume_factor * avg_volume:
                return signal("hold", "close crossed above the upper band without volume")
            if position is None:
                return signal("buy", "close crossed above the upper band on volume")
            return signal("hold", "close crossed above the upper band; already holding")
        if closes[-1] < bands.middle:
            if position is not None:
                return signal("sell", "close is below the middle band")
            return signal("hold", "close is below the middle band; nothing to sell")
        return signal("hold", "no breakout")
