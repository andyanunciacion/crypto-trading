"""Stay flat: hold no position.

What an auto-switching bot (P6-4, D54) runs in a regime that suits none of its strategies, such as
a crash: it sells whatever it holds and never buys. A state, not an event (D31), so a
bot that switched to it while holding sells at its next candle, whichever candle that
is. Under the switch policy `keep`, the strategy that bought the position sells it
instead; `close_first` sells it at once.
"""

from collections.abc import Sequence

from engine.exchange.models import Candle
from engine.strategies.base import Position, Signal, Strategy, StrategyParams


class StayFlatParams(StrategyParams):
    pass


class StayFlat(Strategy[StayFlatParams]):
    name = "stay_flat"
    version = 1
    params_model = StayFlatParams

    @property
    def warmup_candles(self) -> int:
        return 1

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        ts = window[-1].ts
        if position is not None:
            return Signal(action="sell", ts=ts, reason="staying flat: sell what it holds")
        return Signal(action="hold", ts=ts, reason="staying flat")
