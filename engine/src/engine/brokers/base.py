"""The contract both brokers follow (ARCHITECTURE §7, D34).

A broker takes an order the RiskManager has allowed and makes it happen: `PaperBroker`
simulates it against live prices, `LiveBroker` (P9) sends it to the exchange. Callers
see the same `BrokerOrder` either way, so the bot runner doesn't care which it has.

Orders are limit orders only (D22), and each carries our own client order ID. Placing
the same ID twice returns the first order instead of making a second one (D18).
"""

import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from engine.exchange.models import (
    CLIENT_ORDER_ID_PATTERN,
    Balance,
    OrderRequest,
    OrderSide,
)
from engine.strategies.base import Position

Mode = Literal["paper", "live"]
BrokerOrderStatus = Literal["open", "closed", "canceled", "rejected"]


class DuplicateClientOrderId(Exception):
    """A client order ID already belongs to a *different* order: a bug in whoever made it."""


class UnknownOrder(LookupError):
    """No order has that client order ID."""


class _Frozen(BaseModel):
    # Same rules as the exchange models: immutable, no unknown fields, no NaN/Infinity.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class BrokerFill(_Frozen):
    ts: AwareDatetime
    price: Decimal = Field(gt=0)
    amount: Decimal = Field(gt=0)  # in the base currency
    fee: Decimal = Field(ge=0)
    fee_currency: str


class BrokerOrder(_Frozen):
    """An order as stored, with its fills so far."""

    id: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    bot_id: int
    mode: Mode
    exchange: str
    symbol: str
    client_order_id: str
    exchange_order_id: str | None  # None for paper orders
    side: OrderSide
    type: Literal["limit"]
    price: Decimal = Field(gt=0)  # the limit
    amount: Decimal = Field(gt=0)
    filled: Decimal = Field(ge=0)
    status: BrokerOrderStatus  # "closed" means completely filled
    reject_reason: str | None
    reason: dict[str, JsonValue]  # the signal and risk check behind it
    fills: list[BrokerFill]

    @property
    def remaining(self) -> Decimal:
        return self.amount - self.filled


def client_order_id(bot_id: int, signal_ts: datetime, side: OrderSide) -> str:
    """The ID for the order a bot places on the signal for the candle opening at `signal_ts`.

    Deterministic, e.g. `b12-202610231400-b`: the same signal always gets the same ID,
    so placing it again after a timeout or a restart returns the first order instead of
    buying twice.
    """
    if bot_id <= 0:
        raise ValueError(f"bot_id must be positive, not {bot_id}")
    cid = f"b{bot_id}-{signal_ts.astimezone(UTC):%Y%m%d%H%M}-{side[0]}"
    if not re.fullmatch(CLIENT_ORDER_ID_PATTERN, cid):
        raise ValueError(f"client order ID {cid!r} doesn't match {CLIENT_ORDER_ID_PATTERN}")
    return cid


class Broker(ABC):
    mode: ClassVar[Mode]

    @abstractmethod
    async def place_order(
        self, bot_id: int, request: OrderRequest, reason: dict[str, JsonValue]
    ) -> BrokerOrder:
        """Place a limit order. Placing the same client order ID again returns that order.

        An order the exchange (or paper's copy of its rules) refuses, or one there isn't
        enough balance for, comes back with status "rejected" and `reject_reason`.
        Raises `DuplicateClientOrderId` if the ID belongs to a different order.
        """

    @abstractmethod
    async def cancel_order(self, client_order_id: str) -> BrokerOrder:
        """Cancel what's left of an open order. An order that's already done is returned as is."""

    @abstractmethod
    async def get_order(self, client_order_id: str) -> BrokerOrder | None: ...

    @abstractmethod
    async def balances(self, bot_id: int) -> dict[str, Balance]:
        """The bot's balances by currency, open orders' holds in `used`."""

    @abstractmethod
    async def position(self, bot_id: int, symbol: str) -> Position | None:
        """What the bot holds in `symbol`, in the form strategies take; None when flat."""
