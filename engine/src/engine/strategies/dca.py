"""DCA (dollar-cost averaging): for long-term accumulation.

P6-6, D58. Buy the same amount on a fixed schedule, whatever the price, so more coins come when the
price is low and fewer when it's high: the average entry ends up below the average price.
Optionally buy extra when the close falls a set percentage below the last buy (a
"safety order"), and sell everything once it's a set percentage above the average entry.

Each buy is the bot's sizing (`fixed` is the classic "100 USDT each time"). The account's
coin exposure cap (D41) stops it accumulating past its share of the equity.

Stateless (D28):
- The schedule is the candle's open time: every `interval` candles counted from 1970, so a
  bot that restarts, one that started at another hour, and a backtest all buy on the same
  candles (every 24 hourly candles is 00:00 UTC).
- The dip is measured from the position's last buy (`Position.last_entry`), scheduled or
  not: buy more only if it's `dip_percent` cheaper than last time. It's an event (the
  close falling through its level), so a crash that stays down is bought once, and the
  next dip needs a further `dip_percent` fall. Version 1 measured it from the average
  entry, which a small buy hardly moves, so every wobble back across the level bought
  again (D58). Without a last buy (a position made by hand) it's the average entry.
- The take-profit is measured from the average entry, the whole position's cost. It's a
  state, so a bot that missed the crossing candle still sells (D31).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import Field

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position, Signal, Strategy, StrategyParams

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class DcaParams(StrategyParams):
    interval: int = Field(default=24, ge=1, description="Buy every this many candles")
    dip_percent: Decimal = Field(
        default=Decimal(0),
        ge=0,
        lt=100,
        description="Also buy when the close falls this far below the last buy; 0 is off",
    )
    take_profit_percent: Decimal = Field(
        default=Decimal(0),
        ge=0,
        description="Sell everything this far above the average entry; 0 never sells",
    )


class Dca(Strategy[DcaParams]):
    name = "dca"
    version = 2  # 1 measured the dip from the average entry
    params_model = DcaParams
    scales_in = True

    @property
    def warmup_candles(self) -> int:
        # The previous close, for the dip; the gap between the two gives the timeframe.
        return 2

    def decide(self, window: Sequence[Candle], position: Position | None) -> Signal:
        prev, last = window[-2], window[-1]
        close = last.close
        dip, take_profit = self.params.dip_percent, self.params.take_profit_percent
        indicators = {"close": float(close)}
        dip_level = take_profit_level = None
        if position is not None:
            indicators["avg_entry"] = float(position.avg_entry)
            last_buy = position.last_entry
            if last_buy is not None:
                indicators["last_entry"] = float(last_buy)
            if dip > 0:
                reference = position.avg_entry if last_buy is None else last_buy
                dip_level = reference * (1 - dip / 100)
                indicators["dip_level"] = float(dip_level)
            if take_profit > 0:
                take_profit_level = position.avg_entry * (1 + take_profit / 100)
                indicators["take_profit_level"] = float(take_profit_level)

        def signal(action: Action, reason: str) -> Signal:
            return Signal(action=action, ts=last.ts, reason=reason, indicators=indicators)

        if take_profit_level is not None and close >= take_profit_level:
            return signal("sell", f"close is {_show(take_profit)}% above the average entry")
        if dip_level is not None and prev.close >= dip_level > close:
            return signal("buy", f"close fell {_show(dip)}% below the last buy")
        step = last.ts - prev.ts  # the window is contiguous (SignalRunner, the backtester)
        if (last.ts - EPOCH) // step % self.params.interval == 0:
            return signal("buy", f"every {self.params.interval} candles")
        return signal("hold", "not a buying candle")


def _show(value: Decimal) -> str:
    return f"{value.normalize():f}"
