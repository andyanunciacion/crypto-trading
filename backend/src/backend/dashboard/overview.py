"""The Overview: what the dashboard shows at a glance, read fresh each time (D45).

Built by the API from the database and Redis, so it still answers with the engine down;
the engine's heartbeat and each price carry their time, and the dashboard greys out
whatever is too old (DESIGN UX5). Nothing here is stored.

- `account`: the paper account, every bot counted (stopped ones too), in USDT, as the
  RiskManager's limits see it: the same formulas (shared/account.py), so today's P&L
  here is the one the daily loss limit checks (D41). Each open position is valued at
  the latest bid, or at its entry while there's never been a price.
- `positions`: the open ones, each with its P&L so far: what's realized (fees, any part
  sold) plus the move from its average entry to the bid.
- `bots`: all of them, with their status and why they're paused or stopped.
- `prices`: the prices the engine streams (its heartbeat lists them) and every coin
  held. Money is Decimal, sent as JSON strings (D9).
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from shared.account import ClosedTrade, Held, account_totals, manila_midnight
from shared.db.models import BotRow, PaperBalanceRow, PositionRow, StrategyConfigRow
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat
from shared.prices import PriceUpdate, price_key

# Every pair is quoted in USDT (ARCHITECTURE §8), so the account is too.
QUOTE = "USDT"
HUNDRED = Decimal(100)
PERCENT = Decimal("0.01")


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class AccountView(_Out):
    mode: Literal["paper"]
    currency: str
    equity: Decimal  # the cash, plus every open position at the bid
    deposits: Decimal  # what was put in: equity with no trade ever made
    today_pnl: Decimal  # equity - its value at midnight in Manila
    today_pnl_pct: Decimal | None  # of midnight's equity; None when that's 0
    total_pnl: Decimal  # equity - deposits
    total_pnl_pct: Decimal | None  # of the deposits; None when there are none
    peak: Decimal  # the high-water mark over closed trades
    drawdown_pct: Decimal  # how far equity is below the peak; 0 at or above it


class PositionView(_Out):
    bot_id: int
    bot_name: str
    exchange: str
    symbol: str
    amount: Decimal
    avg_entry: Decimal
    bid: Decimal | None  # None: there's never been a price
    price_ts: AwareDatetime | None
    value: Decimal  # at the bid (at the entry with no price)
    pnl: Decimal  # realized so far + amount x (bid - entry)
    pnl_pct: Decimal | None  # of the cost
    stop: Decimal | None  # the exit levels the bot placed (D40)
    take_profit: Decimal | None
    opened_at: AwareDatetime


class BotView(_Out):
    id: int
    name: str
    exchange: str
    symbol: str
    timeframe: str
    mode: str
    status: str
    strategy: str | None
    paused_reason: str | None
    paused_at: AwareDatetime | None
    stopped_reason: str | None
    stopped_at: AwareDatetime | None


class PriceView(_Out):
    exchange: str
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    ts: AwareDatetime | None  # None: never had a price


class Overview(_Out):
    server_time: AwareDatetime  # ages are worked out against this, not the browser's clock
    mode: Literal["paper", "live"]  # the global live gate (D17)
    engine: EngineHeartbeat | None  # None: it has never beaten
    account: AccountView
    positions: list[PositionView]
    bots: list[BotView]
    prices: list[PriceView]


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if whole == 0 else (part / whole * HUNDRED).quantize(PERCENT)


async def read_overview(
    db: AsyncEngine, redis: Redis, *, mode: Literal["paper", "live"], now: datetime
) -> Overview:
    paper_quote = PositionRow.symbol.endswith(f"/{QUOTE}")
    async with db.connect() as conn:
        cash = await conn.scalar(
            select(func.sum(PaperBalanceRow.free + PaperBalanceRow.used)).where(
                PaperBalanceRow.currency == QUOTE
            )
        )
        held = (
            await conn.execute(
                select(
                    PositionRow.bot_id,
                    BotRow.name.label("bot_name"),
                    PositionRow.exchange,
                    PositionRow.symbol,
                    PositionRow.amount,
                    PositionRow.avg_entry,
                    PositionRow.realized_pnl,
                    PositionRow.stop,
                    PositionRow.take_profit,
                    PositionRow.opened_at,
                )
                .join(BotRow, BotRow.id == PositionRow.bot_id)
                .where(PositionRow.mode == "paper", PositionRow.closed_at.is_(None), paper_quote)
                .order_by(PositionRow.opened_at, PositionRow.id)
            )
        ).all()
        closed = (
            await conn.execute(
                select(PositionRow.realized_pnl, PositionRow.closed_at)
                .where(PositionRow.mode == "paper", PositionRow.closed_at.is_not(None), paper_quote)
                .order_by(PositionRow.closed_at, PositionRow.id)
            )
        ).all()
        bots = (
            await conn.execute(
                select(
                    BotRow.id,
                    BotRow.name,
                    BotRow.exchange,
                    BotRow.symbol,
                    BotRow.timeframe,
                    BotRow.mode,
                    BotRow.status,
                    StrategyConfigRow.strategy,
                    BotRow.paused_reason,
                    BotRow.paused_at,
                    BotRow.stopped_reason,
                    BotRow.stopped_at,
                )
                .select_from(BotRow)
                .outerjoin(
                    StrategyConfigRow, StrategyConfigRow.id == BotRow.active_strategy_config_id
                )
                .order_by(BotRow.id)
            )
        ).all()

    raw_beat = await redis.get(ENGINE_HEARTBEAT_KEY)
    engine = None if raw_beat is None else EngineHeartbeat.model_validate_json(raw_beat)
    streamed = [] if engine is None else [(engine.exchange, s) for s in engine.symbols]
    # Then every coin held; dict.fromkeys drops repeats and keeps the order.
    watched = list(dict.fromkeys([*streamed, *((p.exchange, p.symbol) for p in held)]))
    latest = await _latest_prices(redis, watched)

    positions = []
    marked = []
    for p in held:
        price = latest.get((p.exchange, p.symbol))
        bid = None if price is None else price.bid
        mark = p.avg_entry if bid is None else bid
        marked.append(Held(p.amount, p.avg_entry, p.realized_pnl, mark))
        pnl = p.realized_pnl + p.amount * (mark - p.avg_entry)
        positions.append(
            PositionView(
                bot_id=p.bot_id,
                bot_name=p.bot_name,
                exchange=p.exchange,
                symbol=p.symbol,
                amount=p.amount,
                avg_entry=p.avg_entry,
                bid=bid,
                price_ts=None if price is None else price.ts,
                value=p.amount * mark,
                pnl=pnl,
                pnl_pct=_pct(pnl, p.amount * p.avg_entry),
                stop=p.stop,
                take_profit=p.take_profit,
                opened_at=p.opened_at,
            )
        )

    totals = account_totals(
        Decimal(cash or 0),  # SUM of no rows is NULL
        marked,
        [ClosedTrade(c.realized_pnl, c.closed_at) for c in closed],
        manila_midnight(now),
    )
    today = totals.equity - totals.day_start
    total = totals.equity - totals.deposits
    drawdown = _pct(max(totals.peak - totals.equity, Decimal(0)), totals.peak)
    return Overview(
        server_time=now,
        mode=mode,
        engine=engine,
        account=AccountView(
            mode="paper",
            currency=QUOTE,
            equity=totals.equity,
            deposits=totals.deposits,
            today_pnl=today,
            today_pnl_pct=_pct(today, totals.day_start),
            total_pnl=total,
            total_pnl_pct=_pct(total, totals.deposits),
            peak=totals.peak,
            drawdown_pct=Decimal(0) if drawdown is None else drawdown,
        ),
        positions=positions,
        bots=[BotView.model_validate(bot._asdict()) for bot in bots],
        prices=[
            _price_view(exchange, symbol, latest.get((exchange, symbol)))
            for exchange, symbol in watched
        ],
    )


def _price_view(exchange: str, symbol: str, price: PriceUpdate | None) -> PriceView:
    if price is None:
        return PriceView(exchange=exchange, symbol=symbol, bid=None, ask=None, ts=None)
    return PriceView(exchange=exchange, symbol=symbol, bid=price.bid, ask=price.ask, ts=price.ts)


async def _latest_prices(
    redis: Redis, keys: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], PriceUpdate]:
    """The latest `PriceUpdate` of each (exchange, symbol) that has one, in one MGET."""
    if not keys:
        return {}
    raws = await redis.mget([price_key(exchange, symbol) for exchange, symbol in keys])
    return {
        key: PriceUpdate.model_validate_json(raw)
        for key, raw in zip(keys, raws, strict=True)
        if raw is not None
    }
