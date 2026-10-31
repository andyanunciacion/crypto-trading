"""What a trade alert says about an order, read from what the engine stored (D46).

The log line says an order is done; its rows say how it went: every fill (for the
average price and the fees), why it was placed (`reason`: the strategy's signal, or which
stop or the kill switch sold), and for a sell that emptied the position, the trade's
realized P&L, net of fees (D34). `read_trade()` is the trade explanation panel's own
reader, so the message and the panel can't disagree.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.market.explain import read_trade
from shared.db.models import BotRow, OrderRow, PositionRow


@dataclass(frozen=True)
class TradeFacts:
    bot_name: str
    mode: str
    symbol: str
    side: str
    status: str  # closed, or canceled with part of it filled
    filled: Decimal
    avg_price: Decimal  # at the finest precision of its fill prices
    cost: Decimal  # what the fills were worth, fees aside
    fee: Decimal
    fee_currency: str
    strategy: str | None  # whose signal placed it; None for an exit
    signal_reason: str | None
    exit_kind: str | None  # stop_loss, trailing_stop, take_profit, kill_switch, close, switch
    realized_pnl: Decimal | None  # a sell that closed the position: the trade's P&L
    filled_at: datetime  # its last fill


async def read_trade_facts(db: AsyncEngine, client_order_id: str) -> TradeFacts | None:
    """The order's facts, or None if it has no fills (or isn't there)."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(OrderRow.id, OrderRow.mode, BotRow.name)
                .join(BotRow, BotRow.id == OrderRow.bot_id)
                .where(OrderRow.client_order_id == client_order_id)
            )
        ).one_or_none()
        if row is None:
            return None
        trade = await read_trade(conn, row.id)
        if trade is None or not trade.fills:
            return None
        last = trade.fills[-1].ts
        realized = None
        if trade.side == "sell":
            # The broker closes a position at the fill that empties it.
            realized = await conn.scalar(
                select(PositionRow.realized_pnl).where(
                    PositionRow.bot_id == trade.bot_id,
                    PositionRow.symbol == trade.symbol,
                    PositionRow.mode == row.mode,
                    PositionRow.closed_at == last,
                )
            )
    amount = sum((f.amount for f in trade.fills), Decimal(0))
    cost = sum((f.price * f.amount for f in trade.fills), Decimal(0))
    places = min(int(f.price.as_tuple().exponent) for f in trade.fills)
    return TradeFacts(
        bot_name=row.name,
        mode=row.mode,
        symbol=trade.symbol,
        side=trade.side,
        status=trade.status,
        filled=amount,
        avg_price=(cost / amount).quantize(Decimal(1).scaleb(places)),
        cost=cost,
        fee=sum((f.fee for f in trade.fills), Decimal(0)),
        fee_currency=trade.fills[0].fee_currency,
        strategy=None if trade.strategy_config is None else trade.strategy_config.strategy,
        signal_reason=None if trade.signal is None else trade.signal.reason,
        exit_kind=None if trade.exit is None else trade.exit.kind,
        realized_pnl=realized,
        filled_at=last,
    )
