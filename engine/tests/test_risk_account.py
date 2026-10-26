"""PaperAccounts: the account the limits check, worked out from the trading tables.

Against a throwaway TimescaleDB (see shared.testing). Three paper bots, the third one
stopped, at noon in Manila on 2026-10-26 (04:00 UTC); midnight there was 16:00 UTC on
the 25th.
- Bot 1: 500 USDT, and holds 5 BTC bought at 100 (0.5 fee so far). The bid is 90.
- Bot 2: 1100 USDT, 95.095 of it held by an open buy of 1 BTC at 95. Two closed trades:
  +150 just before midnight in Manila, -50 just after.
- Bot 3 (stopped): 1000 USDT, and an open buy of 2 ETH at 50.

Cash 2600. Equity 2600 + 5 x 90 = 3050. At cost: 2600 + 5 x 100 = 3100, which is the
deposits plus every realized P&L (-0.5 + 150 - 50 = 99.5), so the deposits were 3000.5.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import URL, delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.risk.account import PaperAccounts
from engine.risk.breakers import Market, PriceSwings, Swing
from engine.risk.limits import Account, RiskDecision, RiskEvent, RiskSettings
from shared.db.models import (
    BotRow,
    OrderRow,
    PaperBalanceRow,
    PositionRow,
    RiskEventRow,
    RiskSettingsRow,
)
from shared.db.session import make_engine
from shared.prices import PriceUpdate
from shared.testing import insert_bot, set_limits

NOW = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 25, 16, 0, tzinfo=UTC)  # in Manila
BTC = "BTC/USDT"


def d(value: str) -> Decimal:
    return Decimal(value)


@dataclass
class Quotes:
    bids: dict[str, str] = field(default_factory=lambda: {BTC: "90"})
    at: dict[str, datetime] = field(default_factory=dict)  # when each price came; NOW if not

    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        if symbol not in self.bids:
            return None
        bid = d(self.bids[symbol])
        ts = self.at.get(symbol, NOW)
        return PriceUpdate(
            exchange=exchange,
            symbol=symbol,
            bid=bid,
            ask=bid + 1,
            ts=ts,
            received_at=ts,
            source="ws",
        )


def position(bot_id: int, amount: str, closed_at: datetime | None, pnl: str) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "mode": "paper",
        "exchange": "mexc",
        "symbol": BTC,
        "side": "long",
        "amount": d(amount),
        "avg_entry": d("100"),
        "fees_paid": d("0.5"),
        "opened_at": NOW - timedelta(days=1),
        "closed_at": closed_at,
        "realized_pnl": d(pnl),
    }


def open_buy(bot_id: int, symbol: str, amount: str, price: str) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "mode": "paper",
        "exchange": "mexc",
        "symbol": symbol,
        "client_order_id": f"b{bot_id}-202610260400-b",
        "side": "buy",
        "type": "limit",
        "price": d(price),
        "amount": d(amount),
        "filled": d("0"),
        "status": "open",
        "reason": {},
    }


def usdt(bot_id: int, free: str, used: str = "0") -> dict[str, object]:
    return {"bot_id": bot_id, "currency": "USDT", "free": d(free), "used": d(used)}


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events RESTART IDENTITY"
            )
        )
    for bot_id in (1, 2, 3):
        await insert_bot(engine, bot_id)
    async with engine.begin() as conn:
        await conn.execute(update(BotRow).where(BotRow.id == 3).values(status="stopped"))
        await conn.execute(
            insert(PaperBalanceRow),
            [
                usdt(1, "500"),
                {"bot_id": 1, "currency": "BTC", "free": d("5"), "used": d("0")},
                usdt(2, "1004.905", "95.095"),
                usdt(3, "1000"),
            ],
        )
        await conn.execute(
            insert(PositionRow),
            [
                position(1, "5", None, "-0.5"),
                position(2, "0", MIDNIGHT - timedelta(hours=1), "150"),
                position(2, "0", MIDNIGHT + timedelta(hours=1), "-50"),
            ],
        )
        await conn.execute(
            insert(OrderRow),
            [open_buy(2, BTC, "1", "95"), open_buy(3, "ETH/USDT", "2", "50")],
        )
    yield engine
    await engine.dispose()


def accounts(db: AsyncEngine, quotes: Quotes | None = None) -> PaperAccounts:
    return PaperAccounts(db, quotes or Quotes(), clock=lambda: NOW)


async def daily_loss_block(db: AsyncEngine, ts: datetime, rule: str = "daily_loss") -> None:
    async with db.begin() as conn:
        await conn.execute(
            insert(RiskEventRow).values(
                ts=ts,
                mode="paper",
                bot_id=1,
                client_order_id="b1-202610251500-b",
                rule=rule,
                decision="blocked",
                detail="test",
                inputs={},
            )
        )


async def test_the_account_counts_every_bot(db: AsyncEngine) -> None:
    assert await accounts(db).snapshot(BTC) == Account(
        equity=d("3050"),  # 2600 cash + 5 BTC at the 90 bid
        # 3000.5 deposited, +150 by the first closed trade, then -50.
        peak=d("3150.5"),
        day_start=d("3150.5"),  # the +150 closed before midnight in Manila, the -50 after
        # Bot 1's position, plus the two open buys of bots that hold nothing yet.
        open_positions=3,
        coin_exposure=d("545"),  # 5 BTC at 90, plus the open buy of 1 at 95
        daily_loss_hit=False,
    )


async def test_exposure_is_per_coin(db: AsyncEngine) -> None:
    account = await accounts(db, Quotes({BTC: "90", "ETH/USDT": "50"})).snapshot("ETH/USDT")
    assert account.coin_exposure == d("100")  # the open buy of 2 ETH at 50


async def test_a_position_with_no_price_yet_counts_at_its_entry(db: AsyncEngine) -> None:
    account = await accounts(db, Quotes({})).snapshot(BTC)
    assert (account.equity, account.coin_exposure) == (d("3100"), d("595"))


async def test_a_partly_filled_buy_counts_once(db: AsyncEngine) -> None:
    # Bot 2's buy filled 0.4 of its 1 BTC: that much is its position, the rest on order.
    async with db.begin() as conn:
        await conn.execute(update(OrderRow).where(OrderRow.bot_id == 2).values(filled=d("0.4")))
        await conn.execute(insert(PositionRow).values(position(2, "0.4", None, "0")))

    account = await accounts(db).snapshot(BTC)

    assert account.open_positions == 3  # bot 2's buy opened its position: no longer pending
    assert account.coin_exposure == d("5.4") * 90 + d("0.6") * 95


async def test_the_daily_loss_limit_stays_hit_until_midnight_in_manila(db: AsyncEngine) -> None:
    await daily_loss_block(db, MIDNIGHT - timedelta(seconds=1))  # yesterday, in Manila
    await daily_loss_block(db, NOW, rule="max_open_positions")
    assert not (await accounts(db).snapshot(BTC)).daily_loss_hit

    await daily_loss_block(db, MIDNIGHT)
    assert (await accounts(db).snapshot(BTC)).daily_loss_hit


async def test_settings_come_from_the_mode_row(db: AsyncEngine) -> None:
    await set_limits(
        db,
        max_open_positions=2,
        max_drawdown_pct=d("7.5"),
        stale_after_candles=2,
        spike_pct=d("3"),
        spike_minutes=5,
    )

    assert await accounts(db).settings() == RiskSettings(
        max_open_positions=2,
        max_drawdown_pct=d("7.5"),
        stale_after_candles=2,
        spike_pct=d("3"),
        spike_minutes=5,
    )


async def test_the_database_refuses_half_a_breaker(db: AsyncEngine) -> None:
    with pytest.raises(IntegrityError, match="ck_risk_settings_breakers"):
        await set_limits(db, max_failed_orders=3)


# --- The circuit breakers' market (D42) ------------------------------------------------

ETH = "ETH/USDT"
SPIKES = RiskSettings(spike_pct=d("3"), spike_minutes=5)


def mid(ts: datetime, price: str, symbol: str = ETH) -> PriceUpdate:
    return PriceUpdate(
        exchange="mexc",
        symbol=symbol,
        bid=d(price),
        ask=d(price),
        ts=ts,
        received_at=ts,
        source="ws",
    )


async def test_the_market_has_every_price_the_buy_relies_on(db: AsyncEngine) -> None:
    # Bot 2 buys ETH. Bot 1 holds BTC, whose price is 90 s old; ETH's came just now.
    quotes = Quotes({BTC: "90", ETH: "50"}, at={BTC: NOW - timedelta(seconds=90)})
    swings = PriceSwings()
    for minutes_ago, price in ((6, "40"), (4, "50"), (1, "51.5")):  # 6 is outside the 5 min
        swings.observe(mid(NOW - timedelta(minutes=minutes_ago), price))
    swings.observe(mid(NOW, "70", symbol=BTC))  # another coin's swing doesn't count

    market = await PaperAccounts(db, quotes, clock=lambda: NOW, swings=swings).market(
        2, ETH, SPIKES
    )

    assert market == Market(
        status="running",
        paused_reason=None,
        timeframe="1m",
        price_ages={ETH: timedelta(0), BTC: timedelta(seconds=90)},  # the one bought first
        swing=Swing(low=d("50"), high=d("51.5")),
    )


async def test_a_coin_never_priced_has_no_age(db: AsyncEngine) -> None:
    market = await accounts(db, Quotes({})).market(1, BTC, RiskSettings())
    assert (market.price_ages, market.swing) == ({BTC: None}, None)  # spikes off: no swing


async def test_the_market_has_the_bots_status(db: AsyncEngine) -> None:
    await accounts(db).pause(1, "3 failed orders within 10 min")

    paused = await accounts(db).market(1, BTC, RiskSettings())
    stopped = await accounts(db).market(3, BTC, RiskSettings())

    assert (paused.status, paused.paused_reason) == ("paused", "3 failed orders within 10 min")
    assert (stopped.status, stopped.paused_reason) == ("stopped", None)


async def test_a_bot_missing_from_bots_is_an_error(db: AsyncEngine) -> None:
    with pytest.raises(LookupError, match="no paper bot 9"):
        await accounts(db).market(9, BTC, RiskSettings())


async def test_pausing_pauses_only_a_running_bot(db: AsyncEngine) -> None:
    assert await accounts(db).pause(1, "first") is True
    assert await accounts(db).pause(1, "again") is False  # already paused: keeps its reason
    assert await accounts(db).pause(3, "stopped") is False

    async with db.connect() as conn:
        rows = (
            await conn.execute(
                select(BotRow.id, BotRow.status, BotRow.paused_reason, BotRow.paused_at).order_by(
                    BotRow.id
                )
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        (1, "paused", "first", NOW),
        (2, "running", None, None),
        (3, "stopped", None, None),
    ]


async def test_no_settings_row_is_an_error_not_no_limits(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        saved = (
            await conn.execute(select(RiskSettingsRow).where(RiskSettingsRow.mode == "paper"))
        ).one()
        await conn.execute(delete(RiskSettingsRow).where(RiskSettingsRow.mode == "paper"))
    try:
        with pytest.raises(LookupError, match="risk_settings"):
            await accounts(db).settings()
    finally:
        async with db.begin() as conn:
            await conn.execute(insert(RiskSettingsRow).values(saved._asdict()))


async def test_an_event_is_recorded_with_its_inputs(db: AsyncEngine) -> None:
    decision = RiskDecision(decision="blocked", rule="max_open_positions", detail="3 open")
    event = RiskEvent(1, "b1-202610260400-b", decision, {"account": {"open_positions": 3}})

    await accounts(db).record(event)

    async with db.connect() as conn:
        rows = (await conn.execute(select(RiskEventRow))).all()
    assert [
        (r.ts, r.mode, r.bot_id, r.client_order_id, r.rule, r.decision, r.detail, r.inputs)
        for r in rows
    ] == [
        (
            NOW,
            "paper",
            1,
            "b1-202610260400-b",
            "max_open_positions",
            "blocked",
            "3 open",
            {"account": {"open_positions": 3}},
        )
    ]
