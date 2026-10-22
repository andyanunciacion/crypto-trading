"""What the adapter returns: plain, validated data with Decimal money and UTC times.

These are the only exchange types the rest of the engine sees. CCXT's dicts never
leave the adapter.
"""

from decimal import Decimal
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Timeframe = Literal["1m", "5m", "15m", "1h", "4h", "1d"]
OrderSide = Literal["buy", "sell"]
OrderStatus = Literal["open", "closed", "canceled", "expired", "rejected"]

# Letters, digits, '_' and '-', up to 32 characters. Deliberately stricter than the
# exchange needs, since we generate these IDs ourselves.
CLIENT_ORDER_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


class _Frozen(BaseModel):
    # frozen: values can't change after validation. allow_inf_nan=False: a Decimal
    # 'NaN' or 'Infinity' from the exchange is a validation error, never a price.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Balance(_Frozen):
    currency: str
    free: Decimal = Field(ge=0)
    used: Decimal = Field(ge=0)  # locked in open orders
    total: Decimal = Field(ge=0)


class Candle(_Frozen):
    ts: AwareDatetime  # the candle's open time, UTC
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)


class Ticker(_Frozen):
    """The current price of one symbol: best bid and ask, and the last trade.

    MEXC's spot WebSocket sends only the best bid and ask (its "book ticker"), so a
    streamed ticker has no `last`. REST sends all three. Every ticker has at least one.
    """

    symbol: str
    ts: AwareDatetime
    bid: Decimal | None = Field(default=None, gt=0)  # None when the book side is empty
    ask: Decimal | None = Field(default=None, gt=0)
    last: Decimal | None = Field(default=None, gt=0)  # None from the WebSocket

    @model_validator(mode="after")
    def _has_a_price(self) -> Self:
        if self.bid is None and self.ask is None and self.last is None:
            raise ValueError("ticker has no price: no bid, ask or last")
        return self


class MarketRules(_Frozen):
    """The exchange's limits for one symbol. Sizing (P4) must produce orders that fit."""

    symbol: str
    amount_step: Decimal = Field(gt=0)  # amounts must be a multiple of this
    price_step: Decimal = Field(gt=0)  # prices must be a multiple of this
    min_amount: Decimal | None = Field(default=None, ge=0)  # in the base currency
    min_cost: Decimal | None = Field(default=None, ge=0)  # amount x price, in the quote currency


class OrderRequest(_Frozen):
    """A limit order to place. Built by the broker (P9) after the RiskManager allows it."""

    symbol: str
    side: OrderSide
    type: Literal["limit"] = "limit"  # market orders come later (see D22)
    amount: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    client_order_id: str = Field(pattern=CLIENT_ORDER_ID_PATTERN)


class Order(_Frozen):
    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    type: Literal["limit", "market"]
    price: Decimal | None = Field(default=None, gt=0)
    amount: Decimal = Field(gt=0)
    filled: Decimal | None = Field(default=None, ge=0)
    # None when the exchange doesn't say: MEXC's "new order" reply has no status.
    status: OrderStatus | None = None
    ts: AwareDatetime | None = None
