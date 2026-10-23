"""Runs a strategy live: one closed candle in, one signal out (ARCHITECTURE §7, D33).

Keeps the last `warmup_candles` closed candles and hands exactly those to `on_candle`,
as the backtester does (D28), so a live bot and a backtest give the same signal for
the same candle; the parity test (P2-6) checks it on a recorded MEXC session.

Seed it with history from REST or the database at startup, then feed it each candle
as it closes. It logs every signal with its inputs, since strategies don't log.
"""

from collections import deque
from collections.abc import Sequence
from itertools import pairwise

import structlog

from engine.exchange.models import Candle, Timeframe
from engine.marketdata.timeframes import STEP
from engine.strategies.base import Position, Signal, Strategy, StrategyParams

log = structlog.get_logger()


class SignalRunner[P: StrategyParams]:
    def __init__(self, strategy: Strategy[P], symbol: str, timeframe: Timeframe) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.timeframe = timeframe
        self._window: deque[Candle] = deque(maxlen=max(strategy.warmup_candles, 1))

    @property
    def window(self) -> list[Candle]:
        """The candles the next signal will be based on, oldest first."""
        return list(self._window)

    def seed(self, history: Sequence[Candle]) -> None:
        """Load closed candles from before the live ones, oldest first."""
        for candle in history:
            self._append(candle)

    def on_closed(self, candle: Candle, position: Position | None) -> Signal:
        """The signal for `candle`, which has just closed."""
        self._append(candle)
        window = list(self._window)
        step = STEP[self.timeframe]
        if any(b.ts - a.ts != step for a, b in pairwise(window)):
            # A candle the exchange never had (D25). Indicators across the hole would
            # mix unrelated prices, and the backtester refuses such a range outright.
            signal = Signal(
                action="hold",
                ts=candle.ts,
                reason=f"gap in the last {len(window)} candles; waiting for a whole window",
            )
        else:
            signal = self.strategy.on_candle(window, position)
        # HOLD is the usual answer, every candle; BUY/SELL are what someone looks for.
        level = log.debug if signal.action == "hold" else log.info
        level(
            "strategy.signal",
            strategy=self.strategy.name,
            version=self.strategy.version,
            params=self.strategy.params.model_dump(mode="json"),
            symbol=self.symbol,
            timeframe=self.timeframe,
            ts=signal.ts.isoformat(),
            action=signal.action,
            reason=signal.reason,
            indicators=signal.indicators,
            holding=position is not None,
        )
        return signal

    def _append(self, candle: Candle) -> None:
        if self._window and candle.ts <= self._window[-1].ts:
            raise ValueError(
                f"candle at {candle.ts:%Y-%m-%d %H:%M} UTC must come after the last one, "
                f"at {self._window[-1].ts:%Y-%m-%d %H:%M} UTC"
            )
        self._window.append(candle)
