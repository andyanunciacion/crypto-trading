"""The plug-in contract every strategy follows (ARCHITECTURE §7).

A strategy turns closed candles into a signal. It never places orders or touches the
exchange (D7), and it keeps no state between calls (D28): a signal depends only on the
last `warmup_candles` candles, so a backtest, a live bot and a bot that just restarted
all give the same signal for the same candle.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from engine.exchange.models import Candle

Action = Literal["buy", "sell", "hold"]


class _Frozen(BaseModel):
    # Same rules as the exchange models: immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Position(_Frozen):
    """What the bot holds in the symbol; `None` in its place means flat.

    Spot only, so a position is always long. A minimal stand-in until P3 stores positions.
    """

    amount: Decimal = Field(gt=0)
    avg_entry: Decimal = Field(gt=0)
    # What the last buy into it paid (its fills' average): DCA measures its dips from it
    # (D58). None when no buy of the bot is known (a position made by hand).
    last_entry: Decimal | None = Field(default=None, gt=0)


class Signal(_Frozen):
    """A strategy's decision for one closed candle.

    `reason` and `indicators` are the inputs behind it, so the caller can log every
    signal with them ("why did it buy?"). Strategies don't log; they stay pure.
    """

    action: Action
    ts: AwareDatetime  # open time of the candle this signal is for
    reason: str
    indicators: dict[str, float] = Field(default_factory=dict)
    # Optional hints for the RiskManager, which decides the real size and stop (P4).
    # On a BUY, the fraction of the normal size (the bot's sizing). On a SELL, the fraction
    # of the position to sell (P6-6); None sells it all.
    size_hint: Decimal | None = Field(default=None, gt=0, le=1)
    stop: Decimal | None = Field(default=None, gt=0)  # stop-loss price


class StrategyParams(_Frozen):
    """Base for a strategy's settings.

    Validated like any boundary input; `model_json_schema()` is what the dashboard
    renders as the settings form (P5), so give each field a description.
    """


class Strategy[P: StrategyParams](ABC):
    name: ClassVar[str]
    version: ClassVar[int]  # bump when the logic changes, so stored results stay comparable
    params_model: ClassVar[type[StrategyParams]]
    # Whether it may BUY while holding, adding to its position (grid, DCA; P6-6, D58).
    # Every other strategy buys only when flat: a BUY while holding is skipped.
    scales_in: ClassVar[bool] = False

    def __init__(self, params: P) -> None:
        self.params = params

    @property
    @abstractmethod
    def warmup_candles(self) -> int:
        """How many closed candles the strategy needs before it can signal."""

    def on_candle(self, candles: Sequence[Candle], position: Position | None) -> Signal:
        """The signal for the last of `candles`: closed candles, oldest first.

        Holds until there are `warmup_candles` of them, then decides on exactly the last
        `warmup_candles`, however much history the caller passes.
        """
        if not candles:
            raise ValueError("on_candle needs at least one candle")
        if len(candles) < self.warmup_candles:
            return Signal(action="hold", ts=candles[-1].ts, reason="warming up")
        return self.decide(candles[-self.warmup_candles :], position)

    @abstractmethod
    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        """The strategy's rule, given exactly `warmup_candles` closed candles."""
