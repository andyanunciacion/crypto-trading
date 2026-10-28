"""Regimes and a bot's auto-switching settings (P6-3, P6-4; D53, D54).

The regime detector (engine/regime/detector.py) labels each closed candle with one of
`Regime`. A bot with auto-switching on (`bots.auto_switch`) maps each regime but
`unclear` to the strategy config that suits it, and switches to it once the regime has
held for `min_hold` closed candles in a row (engine/regime/switcher.py).

These are plain models, here rather than in the engine, so the API can check what the
dashboard sends and show what's stored without importing the engine (as `shared/risk.py`
does for the risk limits, D50).
"""

from typing import Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from shared.commands import SwitchPolicy

Regime = Literal["trending_up", "trending_down", "ranging", "high_volatility", "unclear"]
# Every regime but `unclear`, the dead band: on it the bot keeps the config it has.
MappedRegime = Literal["trending_up", "trending_down", "ranging", "high_volatility"]
MAPPED_REGIMES: tuple[MappedRegime, ...] = get_args(MappedRegime)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class RegimeParams(_Frozen):
    adx_period: int = Field(default=14, ge=1, description="ADX and DI period, in candles")
    trend_adx: float = Field(
        default=25, gt=0, le=100, description="ADX at or above this is a trend"
    )
    range_adx: float = Field(default=20, gt=0, le=100, description="ADX below this is a range")
    ema_period: int = Field(default=50, ge=2, description="EMA period for the slope, in candles")
    slope_candles: int = Field(
        default=10, ge=1, description="How many candles back the EMA's slope is measured"
    )
    atr_period: int = Field(default=14, ge=1, description="ATR period, in candles")
    vol_lookback: int = Field(
        default=100, ge=1, description="Candles of ATR% the current one is compared with"
    )
    vol_multiple: float = Field(
        default=1.5, gt=1, description="ATR% at this many times its average is high volatility"
    )

    @model_validator(mode="after")
    def _range_below_trend(self) -> Self:
        if self.range_adx > self.trend_adx:
            raise ValueError(
                f"range_adx ({self.range_adx}) can't be above trend_adx ({self.trend_adx})"
            )
        return self


class MappedConfig(_Frozen):
    """The strategy and params a regime switches the bot to.

    `version` is the strategy's version when the mapping was saved. The dashboard sends
    none; the engine checks the params, fills it in and stores it, so a stored mapping
    always has one, and one whose strategy has moved on is refused when the bot loads.
    """

    strategy: str = Field(min_length=1, max_length=64)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)


class AutoSwitchSettings(_Frozen):
    """A bot's auto-switching (`bots.auto_switch`; NULL is off)."""

    mapping: dict[MappedRegime, MappedConfig]
    min_hold: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Closed candles in a row a new regime must read before the bot switches",
    )
    # What a switch does with an open position, as for a switch from the Bots page (D51).
    policy: SwitchPolicy = "keep"
    # The detector's settings; the dashboard doesn't edit them yet, so they're the defaults.
    detector: RegimeParams = Field(default_factory=RegimeParams)

    @model_validator(mode="after")
    def _every_regime_mapped(self) -> Self:
        missing = [regime for regime in MAPPED_REGIMES if regime not in self.mapping]
        if missing:
            raise ValueError(f"no strategy for {', '.join(missing)}: map every regime")
        return self
