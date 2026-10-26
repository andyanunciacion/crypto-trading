"""The paper account the limits check (limits.py, D41): its settings, numbers and events.

Account-wide: every paper bot counts, stopped ones too, as if their cash sat on one
exchange account, the way live bots will share one MEXC account (P9). Nothing extra is
stored for it: each buy works the numbers out from the trading tables, so they can't
drift from the trades themselves (as backtest metrics aren't stored, D30).

In the quote currency of the symbol bought (USDT):
- equity, peak and day_start: from shared/account.py, which the dashboard uses too
  (D45). Each open position is valued at the bid, or at its entry if there's never been
  a price (though then the stale data breaker blocks the buy first).
- open positions: the open ones, plus buys still open for a bot with no position yet.
- coin exposure: the coin's open positions at the bid, plus what's left of open buys of
  it at their limit.
- whether the daily loss limit already blocked a buy today, from `risk_events`.

The circuit breakers (breakers.py, D42) get the buying bot's status and timeframe, how
old each price the buy relies on is, and the coin's recent swing from `PriceSwings`.
The error breaker pauses a bot here, in `bots`.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from engine.brokers.base import Mode
from engine.exchange.models import Timeframe
from engine.marketdata.quotes import QuoteSource
from engine.risk.breakers import Market, PriceSwings
from engine.risk.limits import DAILY_LOSS, Account, RiskEvent, RiskSettings
from shared.account import ClosedTrade, Held, account_totals, manila_midnight
from shared.db.models import (
    BotRow,
    OrderRow,
    PaperBalanceRow,
    PositionRow,
    RiskEventRow,
    RiskSettingsRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PaperAccounts:
    mode: Mode = "paper"

    def __init__(
        self,
        db: AsyncEngine,
        quotes: QuoteSource,
        *,
        clock: Callable[[], datetime] = _utcnow,
        swings: PriceSwings | None = None,
    ) -> None:
        self._db = db
        self._quotes = quotes
        self._clock = clock
        self._swings = swings or PriceSwings()  # filled by the price feed (PaperDesk)

    async def settings(self) -> RiskSettings:
        """The limits and breakers in `risk_settings`, read fresh, so a change applies to
        the next buy.

        A missing row is an error, not "no limits": the buy fails rather than going unchecked.
        """
        query = select(
            RiskSettingsRow.max_open_positions,
            RiskSettingsRow.max_coin_exposure_pct,
            RiskSettingsRow.max_daily_loss_pct,
            RiskSettingsRow.max_drawdown_pct,
            RiskSettingsRow.stale_after_candles,
            RiskSettingsRow.max_failed_orders,
            RiskSettingsRow.failed_orders_minutes,
            RiskSettingsRow.spike_pct,
            RiskSettingsRow.spike_minutes,
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

        totals = account_totals(
            Decimal(total_cash or 0),  # SUM of no rows is NULL
            [
                Held(p.amount, p.avg_entry, p.realized_pnl, bids[(p.exchange, p.symbol)])
                for p in held
            ],
            [ClosedTrade(c.realized_pnl, c.closed_at) for c in closed],
            midnight,
        )
        at_bid = [(p, p.amount * bids[(p.exchange, p.symbol)]) for p in held]

        holding = {(p.bot_id, p.symbol) for p in held}
        opening = {(b.bot_id, b.symbol) for b in buys} - holding
        exposure = sum(
            (value for p, value in at_bid if p.symbol.split("/")[0] == coin), Decimal(0)
        ) + sum(
            ((b.amount - b.filled) * b.price for b in buys if b.symbol.split("/")[0] == coin),
            Decimal(0),
        )
        return Account(
            equity=totals.equity,
            peak=totals.peak,
            day_start=totals.day_start,
            open_positions=len(holding) + len(opening),
            coin_exposure=exposure,
            daily_loss_hit=daily_loss_hit,
        )

    async def market(self, bot_id: int, symbol: str, settings: RiskSettings) -> Market:
        """What the circuit breakers check a buy of `symbol` by `bot_id` against.

        A bot missing from `bots` is an error: the buy fails rather than going unchecked.
        """
        async with self._db.connect() as conn:
            bot = (
                await conn.execute(
                    select(
                        BotRow.exchange, BotRow.timeframe, BotRow.status, BotRow.paused_reason
                    ).where(BotRow.id == bot_id, BotRow.mode == self.mode)
                )
            ).one_or_none()
            held = (
                await conn.execute(
                    select(PositionRow.exchange, PositionRow.symbol)
                    .where(PositionRow.mode == self.mode, PositionRow.closed_at.is_(None))
                    .distinct()
                    .order_by(PositionRow.symbol)
                )
            ).all()
        if bot is None:
            raise LookupError(f"no {self.mode} bot {bot_id}")

        now = self._clock()
        # The coin bought, then every coin held: equity values each at its bid.
        ages: dict[str, timedelta | None] = {}
        for exchange, priced in [(bot.exchange, symbol), *((h.exchange, h.symbol) for h in held)]:
            if priced not in ages:
                latest = await self._quotes.latest(exchange, priced)
                ages[priced] = None if latest is None else now - latest.ts
        minutes = settings.spike_minutes
        swing = (
            None
            if minutes is None
            else self._swings.swing(bot.exchange, symbol, now - timedelta(minutes=minutes))
        )
        return Market(
            status=bot.status,
            paused_reason=bot.paused_reason,
            timeframe=cast(
                Timeframe, bot.timeframe
            ),  # the table's check constraint allows no other
            price_ages=ages,
            swing=swing,
        )

    async def pause(self, bot_id: int, reason: str) -> bool:
        """Pause a running bot, saying why (the error breaker). False if it wasn't running."""
        async with self._db.begin() as conn:
            paused = await conn.scalar(
                update(BotRow)
                .where(BotRow.id == bot_id, BotRow.mode == self.mode, BotRow.status == "running")
                .values(status="paused", paused_reason=reason, paused_at=self._clock())
                .returning(BotRow.id)
            )
        return paused is not None

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
