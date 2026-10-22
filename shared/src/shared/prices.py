"""Live prices in Redis: where the engine writes them, and what each update looks like.

The engine (engine/marketdata/prices.py) is the only writer. Readers GET the key for
the latest price, or SUBSCRIBE to the channel for every change (`PSUBSCRIBE prices:*`
for all symbols). Both hold the same JSON: a `PriceUpdate`, money as strings.

Prices never expire. Readers compare `ts` with the clock to decide whether a price is
stale (DESIGN UX5, ARCHITECTURE §6), so an old price shows up as old, not as missing.
"""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

PriceFeed = Literal["ws", "rest"]  # WebSocket stream, or the REST fallback


class PriceUpdate(BaseModel):
    # Same rules as the exchange models: immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    exchange: str  # CCXT id, e.g. "mexc"
    symbol: str  # CCXT format, e.g. "BTC/USDT"
    bid: Decimal | None = Field(default=None, gt=0)  # best price someone will buy at
    ask: Decimal | None = Field(default=None, gt=0)  # best price someone will sell at
    last: Decimal | None = Field(default=None, gt=0)  # last trade; the WebSocket doesn't send it
    ts: AwareDatetime  # the exchange's time for this price: use it to judge staleness
    received_at: AwareDatetime  # when the engine received it
    source: PriceFeed


def price_key(exchange: str, symbol: str) -> str:
    """The Redis key holding the latest `PriceUpdate`, e.g. `price:mexc:BTC/USDT`."""
    return f"price:{exchange}:{symbol}"


def price_channel(exchange: str, symbol: str) -> str:
    """The pub/sub channel that gets every `PriceUpdate`, e.g. `prices:mexc:BTC/USDT`."""
    return f"prices:{exchange}:{symbol}"
