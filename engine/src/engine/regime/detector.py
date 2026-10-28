"""Label each closed candle with the market's regime (ARCHITECTURE §7, D53).

The auto-switcher (P6-4) maps a regime to the strategy that suits it: EMA crossover
for a trend, RSI mean reversion for a range. Three measures decide it, in order:

1. Volatility: ATR as a percent of the close, against its average over the candles
   before. Relative to the coin's own history, so one setting works for BTC on 1d and
   an altcoin on 1h. It comes first: a crash is also a downtrend, but the switcher
   should treat it as a crash.
2. Trend strength: ADX at or above `trend_adx`, with the direction confirmed twice, by
   the DI lines and by the EMA's slope; if they disagree (a trend turning), it's unclear.
3. No trend: ADX below `range_adx` is a range.

ADX between the two lines is `unclear` too, and the switcher keeps its current regime
on it: a dead band, so a label hovering around one threshold can't flip-flop, while the
detector stays stateless (D28): it decides on exactly the last `warmup_candles`, like a
strategy, so a backtest, a live bot and a bot that just restarted agree.
"""

from collections.abc import Sequence
from typing import ClassVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from engine.exchange.models import Candle
from shared.indicators import adx, atr, ema
from shared.regime import Regime, RegimeParams

__all__ = ["Regime", "RegimeDetector", "RegimeParams", "RegimeReading"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class RegimeReading(_Frozen):
    """The regime of one closed candle, with the values behind it (as a `Signal` has)."""

    regime: Regime
    ts: AwareDatetime  # open time of the candle it's for
    reason: str
    indicators: dict[str, float] = Field(default_factory=dict)


class RegimeDetector:
    name: ClassVar[str] = "regime"
    version: ClassVar[int] = 1  # bump when the rules change, as a strategy's

    def __init__(self, params: RegimeParams) -> None:
        self.params = params

    @property
    def warmup_candles(self) -> int:
        p = self.params
        # Wilder's smoothing (ADX twice over, ATR) and the EMA never fully forget their
        # seeds; give each about 6 periods (Wilder's) or 3 (EMA, as EMA crossover does)
        # to fade before any value that's used.
        directional = 8 * p.adx_period + 1
        slope = 3 * p.ema_period + p.slope_candles + 1
        volatility = 7 * p.atr_period + p.vol_lookback + 1
        return max(directional, slope, volatility)

    def detect(self, candles: Sequence[Candle]) -> RegimeReading:
        """The regime of the last of `candles`: closed candles, oldest first."""
        if not candles:
            raise ValueError("detect needs at least one candle")
        if len(candles) < self.warmup_candles:
            return RegimeReading(regime="unclear", ts=candles[-1].ts, reason="warming up")
        return self._decide(candles[-self.warmup_candles :])

    def _decide(self, window: Sequence[Candle]) -> RegimeReading:
        p = self.params
        highs = [float(candle.high) for candle in window]
        lows = [float(candle.low) for candle in window]
        closes = [float(candle.close) for candle in window]

        directional = adx(highs, lows, closes, p.adx_period)[-1]
        average = ema(closes, p.ema_period)
        # Percent per candle, so it reads the same at any price.
        slope = (average[-1] / average[-1 - p.slope_candles] - 1) * 100 / p.slope_candles
        # atr()'s result[i] belongs to candle i + atr_period.
        atr_pct = [
            100 * value / closes[i + p.atr_period]
            for i, value in enumerate(atr(highs, lows, closes, p.atr_period))
        ]
        history = atr_pct[-1 - p.vol_lookback : -1]
        atr_pct_avg = sum(history) / len(history)

        indicators = {
            "adx": directional.adx,
            "plus_di": directional.plus_di,
            "minus_di": directional.minus_di,
            "ema_slope_pct": slope,
            "atr_pct": atr_pct[-1],
            "atr_pct_avg": atr_pct_avg,
        }
        if atr_pct_avg > 0:
            indicators["vol_ratio"] = atr_pct[-1] / atr_pct_avg
            volatile = indicators["vol_ratio"] >= p.vol_multiple
        else:
            # Flat for the whole lookback: any movement at all is far beyond normal.
            volatile = atr_pct[-1] > 0
        ts = window[-1].ts

        def reading(regime: Regime, reason: str) -> RegimeReading:
            return RegimeReading(regime=regime, ts=ts, reason=reason, indicators=indicators)

        strength = directional.adx
        if volatile:
            return reading(
                "high_volatility",
                f"ATR is {atr_pct[-1]:.2f}% of the price, against {atr_pct_avg:.2f}% "
                f"on average over the last {p.vol_lookback} candles",
            )
        if strength >= p.trend_adx:
            up = directional.plus_di > directional.minus_di and slope > 0
            down = directional.minus_di > directional.plus_di and slope < 0
            if up:
                return reading(
                    "trending_up", f"ADX {strength:.1f}, +DI above -DI and the EMA rising"
                )
            if down:
                return reading(
                    "trending_down", f"ADX {strength:.1f}, -DI above +DI and the EMA falling"
                )
            return reading(
                "unclear", f"ADX {strength:.1f}, but the DI lines and the EMA's slope disagree"
            )
        if strength < p.range_adx:
            return reading("ranging", f"ADX {strength:.1f}, below {p.range_adx:g}: no trend")
        return reading(
            "unclear", f"ADX {strength:.1f}, between {p.range_adx:g} and {p.trend_adx:g}"
        )
