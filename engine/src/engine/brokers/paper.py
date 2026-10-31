"""Paper trading: orders filled against live prices, with virtual balances (D34).

Fills come from the latest book ticker the price stream keeps in Redis, sized by what
is on offer at the best price (matching.py). Balances and positions follow ledger.py.
Everything is stored in Postgres (`orders`, `fills`, `positions`, `paper_balances`),
so a restarted engine carries on from where it was. Two things only matching needs stay
in memory: which open orders are still taking (they crossed when placed), and how much
of each side's best price our fills took. A restart forgets both, which is safe: a
taking order then rests at its limit (pessimistic), and the size at the best price
counts once more, on one quote at most. A match claims its size before its transaction
commits, so a rolled-back fill leaves that size counted as taken until the best price
moves: fewer fills for a moment, never more. A rolled-back order stops taking.

Each operation is one database transaction: an order, its reservation and its first
fills are written together or not at all. Balance and order rows are locked
(`SELECT ... FOR UPDATE`) while they change, so two updates can't both spend the same
cash. Locks are always taken orders first, then balances (by bot, then currency), then
the position, so concurrent operations queue up instead of deadlocking.

A price older than `max_quote_age` doesn't fill anything: the order waits for a fresh one.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog
from pydantic import JsonValue
from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from engine.backtest.models import DEFAULT_FEE_RATE
from engine.brokers.base import (
    Broker,
    BrokerFill,
    BrokerOrder,
    DuplicateClientOrderId,
    UnknownOrder,
)
from engine.brokers.ledger import (
    Funds,
    InsufficientFunds,
    PositionState,
    apply_fill,
    release,
    reservation,
    reserve,
    settle,
)
from engine.brokers.matching import Match, Resting, Taken, crosses, forget_moved, match
from engine.exchange.models import Balance, MarketRules, OrderRequest, OrderSide, order_problems
from engine.marketdata.quotes import MAX_QUOTE_AGE, QuoteSource
from engine.strategies.base import Position
from shared.db.models import FillRow, OrderRow, PaperBalanceRow, PositionRow
from shared.prices import PriceUpdate

log = structlog.get_logger()


class RulesSource(Protocol):
    """The part of ExchangeAdapter the PaperBroker uses: public market data only."""

    exchange_id: str

    async def market_rules(self, symbol: str) -> MarketRules: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Open:
    """An open order, as a fill needs it."""

    id: int
    bot_id: int
    client_order_id: str
    symbol: str
    side: OrderSide
    limit: Decimal
    amount: Decimal
    filled: Decimal

    @classmethod
    def from_row(cls, row: Any) -> "_Open":
        return cls(
            id=row.id,
            bot_id=row.bot_id,
            client_order_id=row.client_order_id,
            symbol=row.symbol,
            side=row.side,
            limit=row.price,
            amount=row.amount,
            filled=row.filled,
        )

    def resting(self, *, taking: bool) -> Resting:
        return Resting(
            key=self.client_order_id,
            side=self.side,
            limit=self.limit,
            remaining=self.amount - self.filled,
            taking=taking,
        )


class PaperBroker(Broker):
    mode = "paper"

    def __init__(
        self,
        db: AsyncEngine,
        quotes: QuoteSource,
        rules: RulesSource,
        *,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
        max_quote_age: timedelta = MAX_QUOTE_AGE,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._db = db
        self._quotes = quotes
        self._rules = rules
        self._fee_rate = fee_rate
        self._max_quote_age = max_quote_age
        self._clock = clock
        # Matching state (module docstring). Each match reads and replaces it with no
        # `await` in between, so place_order and on_price can't both spend the same
        # quote; that's also why it changes before the transaction commits. Anything
        # after an `await` uses its own match's result, never these.
        self._taking: set[str] = set()  # client order IDs
        self._taken: dict[str, dict[OrderSide, Taken]] = {}  # by symbol

    @property
    def exchange(self) -> str:
        return self._rules.exchange_id

    # --- Funding ----------------------------------------------------------------------

    async def deposit(self, bot_id: int, currency: str, amount: Decimal) -> Balance:
        """Add virtual money to a paper bot's balance."""
        if amount <= 0:
            raise ValueError(f"deposit must be positive, not {amount}")
        async with self._db.begin() as conn:
            funds = await self._lock_funds(conn, bot_id, [currency])
            funds[currency] = Funds(free=funds[currency].free + amount, used=funds[currency].used)
            await self._save_funds(conn, bot_id, funds)
        log.info("broker.paper.deposit", bot_id=bot_id, currency=currency, amount=str(amount))
        return _balance(currency, funds[currency])

    # --- Orders -----------------------------------------------------------------------

    async def place_order(
        self, bot_id: int, request: OrderRequest, reason: dict[str, JsonValue]
    ) -> BrokerOrder:
        rules = await self._rules.market_rules(request.symbol)
        problems = order_problems(request, rules)  # the exact checks live orders get
        quote = await self._fresh_quote(request.symbol)
        now = self._clock()
        insert_order = (
            insert(OrderRow)
            .values(
                created_at=now,
                updated_at=now,
                bot_id=bot_id,
                mode=self.mode,
                exchange=self.exchange,
                symbol=request.symbol,
                client_order_id=request.client_order_id,
                side=request.side,
                type=request.type,
                price=request.price,
                amount=request.amount,
                filled=Decimal(0),
                status="open",
                reason=reason,
            )
            # The unique client order ID is the duplicate guard (D18). A concurrent
            # insert of the same ID waits for the first to commit, then lands here too.
            .on_conflict_do_nothing(index_elements=["client_order_id"])
            .returning(OrderRow.id)
        )
        order_id = None
        try:
            async with self._db.begin() as conn:
                order_id = await conn.scalar(insert_order)
                if order_id is not None:
                    await self._open(conn, order_id, bot_id, request, problems, quote, rules, now)
        except Exception as exc:
            if order_id is not None:
                # Nothing of it was stored. A retry of the same ID is a new order, and
                # must not inherit this try's taking.
                self._taking.discard(request.client_order_id)
                log.error(
                    "broker.order.rolled_back",
                    mode=self.mode,
                    bot_id=bot_id,
                    client_order_id=request.client_order_id,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
            raise
        if order_id is None:
            return await self._duplicate(bot_id, request)
        return await self._get(request.client_order_id)

    async def on_price(self, update: PriceUpdate) -> list[BrokerFill]:
        """Fill the open orders a new price reaches. The bot runner calls it per update (P3-3)."""
        if update.exchange != self.exchange or not self._is_fresh(update):
            return []
        # Even with nothing open, a moved price ends what we took at the old one.
        self._taken[update.symbol] = forget_moved(self._taken.get(update.symbol, {}), update)
        now = self._clock()
        fills: list[BrokerFill] = []
        async with self._db.begin() as conn:
            rows = (
                await conn.execute(
                    _paper_orders()
                    .where(
                        OrderRow.exchange == self.exchange,
                        OrderRow.symbol == update.symbol,
                        OrderRow.status == "open",
                    )
                    .order_by(OrderRow.id)  # oldest first: equal prices fill in time order
                    .with_for_update()
                )
            ).all()
            if not rows:
                return []
            orders = {row.client_order_id: _Open.from_row(row) for row in rows}
            rules = await self._rules.market_rules(update.symbol)
            resting = [o.resting(taking=o.client_order_id in self._taking) for o in orders.values()]
            # An order this update doesn't reach fills nothing now; live, it would be
            # resting on the book at its limit from here on, so it stops taking.
            self._taking.difference_update(r.key for r in resting if crosses(r, update) is False)
            matched = match(
                resting,
                update,
                amount_step=rules.amount_step,
                taken=self._taken.get(update.symbol),
            )
            self._taken[update.symbol] = matched.taken
            # Amounts are settled by now; applying them bot by bot locks balances in one
            # order everywhere, so concurrent updates for two symbols can't deadlock.
            for m in sorted(matched.matches, key=lambda m: orders[m.key].bot_id):
                fills.append(
                    await self._fill(conn, orders[m.key], m, now, quote=update, taken=matched.taken)
                )
        return fills

    async def cancel_order(self, client_order_id: str) -> BrokerOrder:
        now = self._clock()
        async with self._db.begin() as conn:
            row = (
                await conn.execute(
                    _paper_orders()
                    .where(OrderRow.client_order_id == client_order_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise UnknownOrder(f"no paper order {client_order_id!r}")
            released = None
            self._taking.discard(client_order_id)
            if row.status == "open":
                order = _Open.from_row(row)
                released = await self._release(conn, order, row.amount - row.filled)
                await conn.execute(
                    update(OrderRow)
                    .where(OrderRow.id == row.id)
                    .values(status="canceled", updated_at=now)
                )
        if released is not None:
            log.info(
                "broker.order.canceled",
                mode=self.mode,
                bot_id=row.bot_id,
                client_order_id=client_order_id,
                symbol=row.symbol,
                filled=str(row.filled),
                released=str(released),
            )
        return await self._get(client_order_id)

    async def get_order(self, client_order_id: str) -> BrokerOrder | None:
        async with self._db.connect() as conn:
            return await _load_order(conn, client_order_id)

    # --- Holdings ---------------------------------------------------------------------

    async def balances(self, bot_id: int) -> dict[str, Balance]:
        query = select(PaperBalanceRow.__table__).where(PaperBalanceRow.bot_id == bot_id)
        async with self._db.connect() as conn:
            rows = (await conn.execute(query)).all()
        balances = {
            row.currency: _balance(row.currency, Funds(free=row.free, used=row.used))
            for row in rows
        }
        return {currency: b for currency, b in balances.items() if b.total > 0}

    async def position(self, bot_id: int, symbol: str) -> Position | None:
        """The bot's open position, with what its last buy paid (DCA's dips, D58).

        The last buy is the newest buy order with a fill since the position opened, so a
        previous position's buys never count (as `bots/exits.py`'s `opening_buy`); what it
        paid is its fills' average. A position no buy of the bot filled has none.
        """
        query = select(PositionRow.amount, PositionRow.avg_entry, PositionRow.opened_at).where(
            PositionRow.bot_id == bot_id,
            PositionRow.symbol == symbol,
            PositionRow.mode == self.mode,
            PositionRow.closed_at.is_(None),
        )
        async with self._db.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
            if row is None:
                return None
            since = FillRow.ts >= row.opened_at
            last_buy = (
                select(OrderRow.id)
                .join(FillRow, FillRow.order_id == OrderRow.id)
                .where(
                    OrderRow.bot_id == bot_id,
                    OrderRow.symbol == symbol,
                    OrderRow.mode == self.mode,
                    OrderRow.side == "buy",
                    since,
                )
                .order_by(FillRow.ts.desc(), FillRow.id.desc())
                .limit(1)
                .scalar_subquery()
            )
            fills = (
                await conn.execute(
                    select(FillRow.price, FillRow.amount).where(FillRow.order_id == last_buy, since)
                )
            ).all()
        bought = sum((f.amount for f in fills), Decimal(0))
        paid = sum((f.price * f.amount for f in fills), Decimal(0))
        return Position(
            amount=row.amount,
            avg_entry=row.avg_entry,
            last_entry=paid / bought if bought > 0 else None,
        )

    # --- Internals --------------------------------------------------------------------

    async def _open(
        self,
        conn: AsyncConnection,
        order_id: int,
        bot_id: int,
        request: OrderRequest,
        problems: list[str],
        quote: PriceUpdate | None,
        rules: MarketRules,
        now: datetime,
    ) -> None:
        """Reserve funds for a just-inserted order, and fill what the book allows now."""
        fields = {
            "mode": self.mode,
            "bot_id": bot_id,
            **request.model_dump(mode="json"),
        }
        if problems:
            await self._reject(conn, order_id, "; ".join(problems), now, fields)
            return
        order = _Open(
            id=order_id,
            bot_id=bot_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            limit=request.price,
            amount=request.amount,
            filled=Decimal(0),
        )
        currency = _reserved_currency(order)
        held = reservation(order.side, order.limit, order.amount, self._fee_rate)
        funds = await self._lock_funds(conn, bot_id, [currency])
        try:
            funds[currency] = reserve(funds[currency], held, currency)
        except InsufficientFunds as exc:
            await self._reject(conn, order_id, str(exc), now, fields)
            return
        await self._save_funds(conn, bot_id, funds)
        log.info(
            "broker.order.placed",
            reserved=str(held),
            reserved_currency=currency,
            quote=None if quote is None else quote.model_dump(mode="json"),
            **fields,
        )
        if quote is None:
            return  # no fresh price: it rests until on_price brings one
        resting = order.resting(taking=True)
        if crosses(resting, quote):
            self._taking.add(order.client_order_id)  # _fill drops it once it's closed
        matched = match(
            [resting], quote, amount_step=rules.amount_step, taken=self._taken.get(order.symbol)
        )
        self._taken[order.symbol] = matched.taken
        for m in matched.matches:
            await self._fill(conn, order, m, now, quote=quote, taken=matched.taken)

    async def _fill(
        self,
        conn: AsyncConnection,
        order: _Open,
        m: Match,
        now: datetime,
        *,
        quote: PriceUpdate,
        taken: Mapping[OrderSide, Taken],
    ) -> BrokerFill:
        """Record one fill: the fill row, the order's progress, balances and position.

        `taken` is the `Matched.taken` of the match that gave `m`. `self._taken` can't
        stand in for it: another update may replace it while this awaits the database.
        """
        base, quote_ccy = _currencies(order.symbol)
        funds = await self._lock_funds(conn, order.bot_id, [base, quote_ccy])
        settled = settle(
            order.side,
            order.limit,
            m.price,
            m.amount,
            self._fee_rate,
            base=funds[base],
            quote=funds[quote_ccy],
        )
        funds[base], funds[quote_ccy] = settled.base, settled.quote
        await self._save_funds(conn, order.bot_id, funds)

        fill = BrokerFill(
            ts=now, price=m.price, amount=m.amount, fee=settled.fee, fee_currency=quote_ccy
        )
        await conn.execute(insert(FillRow).values(order_id=order.id, **fill.model_dump()))
        # matching gives an order at most one fill per update, so `order.filled` is current.
        filled = order.filled + m.amount
        status = "closed" if filled == order.amount else "open"
        taking = order.client_order_id in self._taking
        if status == "closed":
            self._taking.discard(order.client_order_id)
        await conn.execute(
            update(OrderRow)
            .where(OrderRow.id == order.id)
            .values(filled=filled, status=status, updated_at=now)
        )
        await self._update_position(conn, order, fill)
        log.info(
            "broker.order.filled",
            mode=self.mode,
            bot_id=order.bot_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            limit=str(order.limit),
            price=str(fill.price),
            amount=str(fill.amount),
            fee=str(fill.fee),
            filled=str(filled),
            status=status,
            taking=taking,
            # All our orders have taken at this side's best price so far, this fill
            # included; it never exceeds the size the quote shows there.
            taken_at_best=str(taken[order.side].amount),
            quote=quote.model_dump(mode="json"),
        )
        return fill

    async def _update_position(self, conn: AsyncConnection, order: _Open, fill: BrokerFill) -> None:
        row = (
            await conn.execute(
                select(PositionRow.__table__)
                .where(
                    PositionRow.bot_id == order.bot_id,
                    PositionRow.symbol == order.symbol,
                    PositionRow.closed_at.is_(None),
                )
                .with_for_update()
            )
        ).one_or_none()
        before = (
            None
            if row is None
            else PositionState(
                amount=row.amount,
                avg_entry=row.avg_entry,
                fees_paid=row.fees_paid,
                realized_pnl=row.realized_pnl,
            )
        )
        after = apply_fill(before, order.side, fill.price, fill.amount, fill.fee)
        values = {
            "amount": after.amount,
            "avg_entry": after.avg_entry,
            "fees_paid": after.fees_paid,
            "realized_pnl": after.realized_pnl,
            "closed_at": fill.ts if after.amount == 0 else None,
        }
        if row is None:
            await conn.execute(
                insert(PositionRow).values(
                    bot_id=order.bot_id,
                    mode=self.mode,
                    exchange=self.exchange,
                    symbol=order.symbol,
                    side="long",
                    opened_at=fill.ts,
                    **values,
                )
            )
        else:
            await conn.execute(update(PositionRow).where(PositionRow.id == row.id).values(**values))

    async def _reject(
        self,
        conn: AsyncConnection,
        order_id: int,
        why: str,
        now: datetime,
        fields: dict[str, Any],
    ) -> None:
        # Kept, not deleted: the record shows what was tried, and a retry of the same
        # client order ID gets the same answer instead of a second try.
        await conn.execute(
            update(OrderRow)
            .where(OrderRow.id == order_id)
            .values(status="rejected", reject_reason=why, updated_at=now)
        )
        log.warning("broker.order.rejected", reject_reason=why, **fields)

    async def _release(self, conn: AsyncConnection, order: _Open, remaining: Decimal) -> Decimal:
        currency = _reserved_currency(order)
        held = reservation(order.side, order.limit, remaining, self._fee_rate)
        funds = await self._lock_funds(conn, order.bot_id, [currency])
        funds[currency] = release(funds[currency], held)
        await self._save_funds(conn, order.bot_id, funds)
        return held

    async def _duplicate(self, bot_id: int, request: OrderRequest) -> BrokerOrder:
        existing = await self._get(request.client_order_id)
        same = (
            existing.mode == self.mode
            and existing.bot_id == bot_id
            and existing.symbol == request.symbol
            and existing.side == request.side
            and existing.type == request.type
            and existing.price == request.price
            and existing.amount == request.amount
        )
        fields = {"mode": self.mode, "bot_id": bot_id, **request.model_dump(mode="json")}
        if not same:
            log.error("broker.order.id_clash", existing=existing.model_dump(mode="json"), **fields)
            raise DuplicateClientOrderId(
                f"client order ID {request.client_order_id!r} is already used by a different order"
            )
        log.info("broker.order.duplicate", status=existing.status, **fields)
        return existing

    async def _get(self, client_order_id: str) -> BrokerOrder:
        order = await self.get_order(client_order_id)
        if order is None:
            raise UnknownOrder(f"no paper order {client_order_id!r}")
        return order

    async def _fresh_quote(self, symbol: str) -> PriceUpdate | None:
        quote = await self._quotes.latest(self.exchange, symbol)
        if quote is None or not self._is_fresh(quote):
            return None
        return quote

    def _is_fresh(self, quote: PriceUpdate) -> bool:
        # `ts` is the exchange's own time for the price (shared/prices.py).
        return self._clock() - quote.ts <= self._max_quote_age

    async def _lock_funds(
        self, conn: AsyncConnection, bot_id: int, currencies: Iterable[str]
    ) -> dict[str, Funds]:
        """The bot's balances in `currencies`, locked until the transaction ends."""
        wanted = sorted(set(currencies))  # one lock order everywhere: no deadlocks
        # Make sure each row exists, so there's something to lock.
        await conn.execute(
            insert(PaperBalanceRow)
            .values([{"bot_id": bot_id, "currency": c, "free": 0, "used": 0} for c in wanted])
            .on_conflict_do_nothing()
        )
        rows = (
            await conn.execute(
                select(PaperBalanceRow.__table__)
                .where(PaperBalanceRow.bot_id == bot_id, PaperBalanceRow.currency.in_(wanted))
                .order_by(PaperBalanceRow.currency)
                .with_for_update()
            )
        ).all()
        return {row.currency: Funds(free=row.free, used=row.used) for row in rows}

    async def _save_funds(
        self, conn: AsyncConnection, bot_id: int, funds: dict[str, Funds]
    ) -> None:
        for currency, f in funds.items():
            await conn.execute(
                update(PaperBalanceRow)
                .where(PaperBalanceRow.bot_id == bot_id, PaperBalanceRow.currency == currency)
                .values(free=f.free, used=f.used)
            )


def _paper_orders() -> Select[Any]:
    # Only paper orders: a PaperBroker can never touch a live one.
    return select(OrderRow.__table__).where(OrderRow.mode == PaperBroker.mode)


async def _load_order(conn: AsyncConnection, client_order_id: str) -> BrokerOrder | None:
    query = _paper_orders().where(OrderRow.client_order_id == client_order_id)
    row = (await conn.execute(query)).mappings().one_or_none()
    if row is None:
        return None
    fills = (
        (
            await conn.execute(
                select(FillRow.ts, FillRow.price, FillRow.amount, FillRow.fee, FillRow.fee_currency)
                .where(FillRow.order_id == row["id"])
                .order_by(FillRow.id)
            )
        )
        .mappings()
        .all()
    )
    return BrokerOrder.model_validate({**row, "fills": [dict(f) for f in fills]})


def _currencies(symbol: str) -> tuple[str, str]:
    base, quote = symbol.split("/")
    return base, quote


def _reserved_currency(order: _Open) -> str:
    base, quote = _currencies(order.symbol)
    return quote if order.side == "buy" else base


def _balance(currency: str, funds: Funds) -> Balance:
    return Balance(
        currency=currency, free=funds.free, used=funds.used, total=funds.free + funds.used
    )
