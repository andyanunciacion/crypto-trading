"""The candle still forming, in Redis, for the dashboard's chart (D46).

The engine watches the exchange's kline stream for every series its bots trade, only to
tell when a candle closed (D33); the closed candle itself comes from REST and is
stored. While a candle is open, the engine also `SET`s the exchange's latest state of
it here, so the chart can move between closes. It's for display only: never stored,
never traded on, and replaced on screen by the stored candle once it closes.

Like prices (shared/prices.py), the key never expires: readers compare `received_at`
with the clock to tell a live candle from a stuck one (DESIGN UX5).
"""

from datetime import timedelta
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# How long each timeframe's candle lasts (the same ones as the database's TIMEFRAMES).
# engine/marketdata/timeframes.py builds its STEP from this.
TIMEFRAME_STEPS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


class FormingCandle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    exchange: str  # CCXT id, e.g. "mexc"
    symbol: str  # CCXT format, e.g. "BTC/USDT"
    timeframe: str  # e.g. "1h"
    ts: AwareDatetime  # the candle's open time
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    received_at: AwareDatetime  # when the engine received this state of it


def forming_key(exchange: str, symbol: str, timeframe: str) -> str:
    """The Redis key holding a series' `FormingCandle`, e.g. `candle:mexc:BTC/USDT:1h`."""
    return f"candle:{exchange}:{symbol}:{timeframe}"
