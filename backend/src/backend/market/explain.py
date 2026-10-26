"""Why a trade happened (DESIGN UX9): its signal, the risk check, and how it filled (D46).

Everything comes from what the engine stored when it placed the order: its `reason`
(the signal and its indicator values, the strategy config, how a buy was sized and
the stops it got, or for an exit which stop or the kill switch fired; then the
RiskManager's decision, D35) and its fills. A resized buy also has its `risk_events`
row, with the settings and account numbers the limit was checked against (D41).

Each part of the reason is read into a typed model; one that doesn't fit (a row from
before a field existed) is left out of the typed parts, and the whole reason is sent
as it is too, so nothing stored is hidden.
"""

from decimal import Decimal
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from shared.db.models import FillRow, OrderRow, RiskEventRow


class _Part(BaseModel):
    # Read from stored JSON: fields added later are ignored rather than refused.
    model_config = ConfigDict(frozen=True, extra="ignore")


class SignalPart(_Part):
    action: str
    ts: AwareDatetime  # the candle it was decided on (its open time)
    reason: str
    indicators: dict[str, float]
    size_hint: Decimal | None = None
    stop: Decimal | None = None


class StrategyPart(_Part):
    id: int
    strategy: str
    version: int
    params: dict[str, JsonValue]


class SizingPart(_Part):
    settings: dict[str, JsonValue]
    detail: str  # how the amount came about


class StopsPart(_Part):
    settings: dict[str, JsonValue]
    atr: Decimal | None  # the ATR at the signal's candle, for ATR stops


class ExitPart(_Part):
    kind: str  # stop_loss, trailing_stop, take_profit, kill_switch
    attempt: int
    bid: Decimal  # the bid it fired at
    price_ts: AwareDatetime
    levels: dict[str, Decimal | None] | None = None  # a stop's levels then
    command: str | None = None  # the kill switch's command id


class RiskPart(_Part):
    decision: str  # allowed, resized (blocked orders are never placed)
    rule: str
    detail: str
    requested_amount: Decimal | None = None  # what was asked for, when resized


class FillView(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: AwareDatetime
    price: Decimal
    amount: Decimal
    fee: Decimal
    fee_currency: str


class RiskEventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: AwareDatetime
    rule: str
    decision: str
    detail: str
    inputs: dict[str, JsonValue]  # the order, the settings and the account numbers


class TradeExplanation(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: int
    bot_id: int
    client_order_id: str
    exchange: str
    symbol: str
    side: str
    price: Decimal  # the limit
    amount: Decimal
    filled: Decimal
    status: str
    created_at: AwareDatetime
    signal: SignalPart | None  # None for an exit
    strategy_config: StrategyPart | None
    sizing: SizingPart | None  # buys only
    stops: StopsPart | None  # buys with stops only
    exit: ExitPart | None  # a stop's or the kill switch's sell
    risk: RiskPart | None
    risk_events: list[RiskEventView]
    fills: list[FillView]
    reason: dict[str, JsonValue]  # all of it, as stored


def _part[P: _Part](model: type[P], reason: dict[str, Any], key: str) -> P | None:
    value = reason.get(key)
    if value is None:
        return None
    try:
        return model.model_validate(value)
    except ValidationError:
        return None


async def read_trade(conn: AsyncConnection, order_id: int) -> TradeExplanation | None:
    order = (await conn.execute(select(OrderRow).where(OrderRow.id == order_id))).one_or_none()
    if order is None:
        return None
    fills = (
        await conn.execute(
            select(FillRow.ts, FillRow.price, FillRow.amount, FillRow.fee, FillRow.fee_currency)
            .where(FillRow.order_id == order_id)
            .order_by(FillRow.ts, FillRow.id)
        )
    ).all()
    events = (
        await conn.execute(
            select(
                RiskEventRow.ts,
                RiskEventRow.rule,
                RiskEventRow.decision,
                RiskEventRow.detail,
                RiskEventRow.inputs,
            )
            .where(
                RiskEventRow.client_order_id == order.client_order_id,
                RiskEventRow.bot_id == order.bot_id,
            )
            .order_by(RiskEventRow.ts, RiskEventRow.id)
        )
    ).all()
    reason: dict[str, Any] = order.reason
    return TradeExplanation(
        order_id=order.id,
        bot_id=order.bot_id,
        client_order_id=order.client_order_id,
        exchange=order.exchange,
        symbol=order.symbol,
        side=order.side,
        price=order.price,
        amount=order.amount,
        filled=order.filled,
        status=order.status,
        created_at=order.created_at,
        signal=_part(SignalPart, reason, "signal"),
        strategy_config=_part(StrategyPart, reason, "strategy_config"),
        sizing=_part(SizingPart, reason, "sizing"),
        stops=_part(StopsPart, reason, "stops"),
        exit=_part(ExitPart, reason, "exit"),
        risk=_part(RiskPart, reason, "risk"),
        risk_events=[RiskEventView.model_validate(e._asdict()) for e in events],
        fills=[FillView.model_validate(f._asdict()) for f in fills],
        reason=reason,
    )
