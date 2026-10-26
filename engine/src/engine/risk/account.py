"""The paper account the limits check (limits.py, D41): its settings, numbers and events.

Account-wide: every paper bot counts, stopped ones too, as if their cash sat on one
exchange account, the way live bots will share one MEXC account (P9). Nothing extra is
stored for it: each buy works the numbers out from the trading tables, so they can't
drift from the trades themselves (as backtest metrics aren't stored, D30).

In the quote currency of the symbol bought (USDT):
- equity: every bot's cash, free and held by orders, plus each open position at the bid
  (at its entry if there's never been a price; a stale price is P4-4's to catch).
- the deposits: cash plus positions at cost always equals what was deposited plus every
  realized P&L (brokers/ledger.py counts each fee when paid), so they're that minus the
  P&L. Adding up the closed trades' P&L from there, in the order they closed, gives the
  equity after each one; the peak is the highest of those. An open trade's loss still
  shows, in equity.
- day_start: the deposits plus the P&L of trades closed before midnight in Manila. An
  open position's whole move since its entry counts as today's, also when it opened
  before midnight: usually the stricter reading.
- open positions: the open ones, plus buys still open for a bot with no position yet.
- coin exposure: the coin's open positions at the bid, plus what's left of open buys of
  it at their limit.
- whether the daily loss limit already blocked a buy today, from `risk_events`.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from engine.brokers.base import Mode
from engine.marketdata.quotes import QuoteSource
from engine.risk.limits import DAILY_LOSS, Account, RiskEvent, RiskSettings, manila_midnight
from shared.db.models import OrderRow, PaperBalanceRow, PositionRow, RiskEventRow, RiskSettingsRow


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PaperAccounts:
    mode: Mode = "paper"

    def __init__(
        self, db: AsyncEngine, quotes: QuoteSource, *, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self._db = db
        self._quotes = quotes
        self._clock = clock

    async def settings(self) -> RiskSettings:
        """The limits in `risk_settings`, read fresh, so a change applies to the next buy.

        A missing row is an error, not "no limits": the buy fails rather than going unchecked.
        """
        query = select(
            RiskSettingsRow.max_open_positions,
            RiskSettingsRow.max_coin_exposure_pct,
            RiskSettingsRow.max_daily_loss_pct,
            RiskSettingsRow.max_drawdown_pct,
        ).where(RiskSettingsRow.mode == self.mode)
        async with self._db.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
        if row is None:
            raise LookupError(f"no {self.mode} row in risk_settings")
        return RiskSettings.model_validate(row._asdict())

    async def snapshot(self, symbol: str) -> Account:
        """The account as a buy of `symbol` finds it (module docstring)."""
        coin, quote = symbol.split("/")
        in_quote = PositionRow.symbol.endswith(f"/{quote}")
        async with self._db.connect() as conn:
            total_cash = await conn.scalar(
                select(func.sum(PaperBalanceRow.free + PaperBalanceRow.used)).where(
                    PaperBalanceRow.currency == quote
                )
            )
            held = (
                await conn.execute(
                    select(
                        PositionRow.bot_id,
                        PositionRow.exchange,
                        PositionRow.symbol,
                        PositionRow.amount,
                        PositionRow.avg_entry,
                        PositionRow.realized_pnl,
                    ).where(
                        PositionRow.mode == self.mode, PositionRow.closed_at.is_(None), in_quote
                    )
                )
            ).all()
            closed = (
                await conn.execute(
                    select(PositionRow.realized_pnl, PositionRow.closed_at)
                    .where(
                        PositionRow.mode == self.mode, PositionRow.closed_at.is_not(None), in_quote
                    )
                    .order_by(PositionRow.closed_at, PositionRow.id)
                )
            ).all()
            buys = (
                await conn.execute(
                    select(
                        OrderRow.bot_id,
                        OrderRow.symbol,
                        OrderRow.price,
                        OrderRow.amount,
                        OrderRow.filled,
                    ).where(
                        OrderRow.mode == self.mode,
                        OrderRow.status == "open",
                        OrderRow.side == "buy",
                        OrderRow.symbol.endswith(f"/{quote}"),
                    )
                )
            ).all()
            midnight = manila_midnight(self._clock())
            daily_loss_hit = await self._daily_loss_hit(conn, midnight)

        bids: dict[tuple[str, str], Decimal] = {}
        for position in held:
            key = (position.exchange, position.symbol)
            if key not in bids:
                latest = await self._quotes.latest(*key)
                bid = None if latest is None else latest.bid
                bids[key] = position.avg_entry if bid is None else bid

        cash = Decimal(total_cash or 0)  # SUM of no rows is NULL
        at_cost = sum((p.amount * p.avg_entry for p in held), Decimal(0))
        at_bid = [(p, p.amount * bids[(p.exchange, p.symbol)]) for p in held]
        realized = sum((p.realized_pnl for p in held), Decimal(0)) + sum(
            (c.realized_pnl for c in closed), Decimal(0)
        )
        deposits = cash + at_cost - realized
        peak = running = deposits
        for trade in closed:
            running += trade.realized_pnl
            peak = max(peak, running)
        day_start = deposits + sum(
            (c.realized_pnl for c in closed if c.closed_at < midnight), Decimal(0)
        )

        holding = {(p.bot_id, p.symbol) for p in held}
        opening = {(b.bot_id, b.symbol) for b in buys} - holding
        exposure = sum(
            (value for p, value in at_bid if p.symbol.split("/")[0] == coin), Decimal(0)
        ) + sum(
            ((b.amount - b.filled) * b.price for b in buys if b.symbol.split("/")[0] == coin),
            Decimal(0),
        )
        return Account(
            equity=cash + sum((value for _, value in at_bid), Decimal(0)),
            peak=peak,
            day_start=day_start,
            open_positions=len(holding) + len(opening),
            coin_exposure=exposure,
            daily_loss_hit=daily_loss_hit,
        )

    async def record(self, event: RiskEvent) -> None:
        """Write a block or resize to `risk_events`."""
        decision = event.decision
        async with self._db.begin() as conn:
            await conn.execute(
                insert(RiskEventRow).values(
                    ts=self._clock(),
                    mode=self.mode,
                    bot_id=event.bot_id,
                    client_order_id=event.client_order_id,
                    rule=decision.rule,
                    decision=decision.decision,
                    detail=decision.detail,
                    inputs=event.inputs,
                )
            )

    async def _daily_loss_hit(self, conn: AsyncConnection, midnight: datetime) -> bool:
        hit = await conn.scalar(
            select(
                exists().where(
                    RiskEventRow.mode == self.mode,
                    RiskEventRow.rule == DAILY_LOSS,
                    RiskEventRow.decision == "blocked",
                    RiskEventRow.ts >= midnight,
                )
            )
        )
        return bool(hit)
