"""The RiskManager: every order is decided on, logged with its inputs and stored with its
decision; blocks and resizes are recorded in `risk_events`, and sells are never blocked.

Unit tests use a recording fake Broker and fake accounts (the limits themselves are
tested in test_limits.py); the last tests put a real PaperBroker and PaperAccounts (on
a throwaway TimescaleDB, see shared.testing) behind it.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import JsonValue
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.brokers.base import Broker, BrokerOrder, DuplicateClientOrderId, Mode
from engine.brokers.paper import PaperBroker
from engine.exchange.models import Balance, MarketRules, OrderRequest
from engine.risk.account import PaperAccounts
from engine.risk.limits import (
    SELLS_ALLOWED,
    WITHIN_LIMITS,
    Account,
    RiskEvent,
    RiskSettings,
)
from engine.risk.manager import RiskManager, RiskOutcome
from engine.strategies.base import Position
from shared.db.models import OrderRow, RiskEventRow
from shared.db.session import make_engine
from shared.prices import PriceUpdate
from shared.testing import insert_bot, set_limits

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
BTC = "BTC/USDT"
BOT = 1
REASON: dict[str, JsonValue] = {"signal": {"action": "buy", "reason": "fast EMA crossed above"}}
WITHIN: JsonValue = {"decision": "allowed", "rule": "within_limits", "detail": "within every limit"}
DEFAULTS = RiskSettings(
    max_open_positions=3,
    max_coin_exposure_pct=Decimal(25),
    max_daily_loss_pct=Decimal(3),
    max_drawdown_pct=Decimal(10),
)
FLAT = Account(
    equity=Decimal(1000),
    peak=Decimal(1000),
    day_start=Decimal(1000),
    open_positions=0,
    coin_exposure=Decimal(0),
    daily_loss_hit=False,
)
ACCOUNT_JSON: JsonValue = {
    "equity": "1000",
    "peak": "1000",
    "day_start": "1000",
    "open_positions": 0,
    "coin_exposure": "0",
    "daily_loss_hit": False,
}
SETTINGS_JSON: JsonValue = {
    "max_open_positions": 3,
    "max_coin_exposure_pct": "25",
    "max_daily_loss_pct": "3",
    "max_drawdown_pct": "10",
}


def order(side: str = "buy", amount: str = "0.5", cid: str = "b1-202610231200-b") -> OrderRequest:
    """0.5 BTC at 100 by default: 50 USDT, well within every limit."""
    return OrderRequest.model_validate(
        {"symbol": BTC, "side": side, "amount": amount, "price": "100", "client_order_id": cid}
    )


def stored(
    bot_id: int, request: OrderRequest, reason: dict[str, JsonValue], status: str = "open"
) -> BrokerOrder:
    return BrokerOrder.model_validate(
        {
            "id": 7,
            "created_at": T0,
            "updated_at": T0,
            "bot_id": bot_id,
            "mode": "paper",
            "exchange": "mexc",
            "symbol": request.symbol,
            "client_order_id": request.client_order_id,
            "exchange_order_id": None,
            "side": request.side,
            "type": request.type,
            "price": request.price,
            "amount": request.amount,
            "filled": "0",
            "status": status,
            "reject_reason": None,
            "reason": reason,
            "fills": [],
        }
    )


class FakeBroker(Broker):
    """Records every call; places nothing. A known client order ID returns its order, or
    raises if the request differs, as the PaperBroker does."""

    mode = "paper"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.placed: list[tuple[int, OrderRequest, dict[str, JsonValue]]] = []
        self.orders: dict[str, BrokerOrder] = {}
        self.canceled: list[str] = []

    async def place_order(
        self, bot_id: int, request: OrderRequest, reason: dict[str, JsonValue]
    ) -> BrokerOrder:
        self.placed.append((bot_id, request, reason))
        if self.error is not None:
            raise self.error
        existing = self.orders.get(request.client_order_id)
        if existing is not None:
            if (existing.side, existing.amount, existing.price) != (
                request.side,
                request.amount,
                request.price,
            ):
                raise DuplicateClientOrderId("already used by a different order")
            return existing
        placed = stored(bot_id, request, reason)
        self.orders[request.client_order_id] = placed
        return placed

    async def cancel_order(self, client_order_id: str) -> BrokerOrder:
        self.canceled.append(client_order_id)
        canceled = self.orders[client_order_id].model_copy(update={"status": "canceled"})
        self.orders[client_order_id] = canceled
        return canceled

    async def get_order(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)

    async def balances(self, bot_id: int) -> dict[str, Balance]:
        assert bot_id == BOT
        cash = Decimal(1000)
        return {"USDT": Balance(currency="USDT", free=cash, used=Decimal(0), total=cash)}

    async def position(self, bot_id: int, symbol: str) -> Position | None:
        assert (bot_id, symbol) == (BOT, BTC)
        return Position(amount=Decimal("0.5"), avg_entry=Decimal(100))


@dataclass
class FakeAccounts:
    settings_: RiskSettings = DEFAULTS
    account: Account = FLAT
    mode: Mode = "paper"
    events: list[RiskEvent] = field(default_factory=list)
    snapshots: int = 0

    async def settings(self) -> RiskSettings:
        return self.settings_

    async def snapshot(self, symbol: str) -> Account:
        assert symbol == BTC
        self.snapshots += 1
        return self.account

    async def record(self, event: RiskEvent) -> None:
        self.events.append(event)


@dataclass
class Rules:
    exchange_id: str = "mexc"

    async def market_rules(self, symbol: str) -> MarketRules:
        return MarketRules(
            symbol=symbol,
            amount_step=Decimal("0.0001"),
            price_step=Decimal("0.01"),
            min_amount=Decimal("0.0001"),
            min_cost=Decimal("1"),
        )


def manager(
    broker: Broker | None = None, accounts: FakeAccounts | None = None
) -> tuple[RiskManager, FakeBroker, FakeAccounts]:
    broker = broker or FakeBroker()
    assert isinstance(broker, FakeBroker)
    accounts = accounts or FakeAccounts()
    return RiskManager(broker, accounts, Rules()), broker, accounts


async def test_a_buy_within_the_limits_passes_unchanged_with_its_decision() -> None:
    risk, broker, accounts = manager()

    outcome = await risk.submit(BOT, order(), REASON)

    # The broker gets exactly the order asked for, and a reason that adds the risk check.
    assert broker.placed == [(BOT, order(), {**REASON, "risk": WITHIN})]
    assert outcome == RiskOutcome(
        decision=WITHIN_LIMITS, order=broker.orders[order().client_order_id]
    )
    assert accounts.events == []  # only blocks and resizes are recorded


async def test_every_buy_is_logged_with_its_inputs() -> None:
    risk, _, _ = manager()

    with capture_logs() as logs:
        await risk.submit(BOT, order(), REASON)

    assert logs == [
        {
            "event": "risk.decision",
            "log_level": "info",
            "mode": "paper",
            "bot_id": BOT,
            "symbol": BTC,
            "side": "buy",
            "type": "limit",
            "amount": "0.5",
            "price": "100",
            "client_order_id": "b1-202610231200-b",
            "reason": REASON,
            "decision": "allowed",
            "rule": "within_limits",
            "detail": "within every limit",
            "settings": SETTINGS_JSON,
            "account": ACCOUNT_JSON,
        }
    ]


BREACHED = [
    pytest.param(replace(FLAT, equity=Decimal(900), day_start=Decimal(900)), "max_drawdown"),
    pytest.param(replace(FLAT, equity=Decimal(970)), "daily_loss"),
    pytest.param(replace(FLAT, open_positions=3), "max_open_positions"),
    pytest.param(replace(FLAT, coin_exposure=Decimal("249.5")), "max_coin_exposure"),
]


@pytest.mark.parametrize(("account", "rule"), BREACHED)
async def test_each_limit_blocks_a_buy_and_records_it(account: Account, rule: str) -> None:
    risk, broker, accounts = manager(accounts=FakeAccounts(account=account))

    with capture_logs() as logs:
        outcome = await risk.submit(BOT, order(), REASON)

    assert outcome.order is None
    assert (outcome.decision.decision, outcome.decision.rule) == ("blocked", rule)
    assert broker.placed == []  # nothing reached the broker
    assert accounts.events == [
        RiskEvent(
            bot_id=BOT,
            client_order_id="b1-202610231200-b",
            decision=outcome.decision,
            inputs={
                "request": order().model_dump(mode="json"),
                "settings": SETTINGS_JSON,
                "account": account.to_json(),
            },
        )
    ]
    assert [(log["event"], log["decision"], log["rule"]) for log in logs] == [
        ("risk.decision", "blocked", rule)
    ]


async def test_a_buy_over_the_coin_cap_is_placed_smaller_and_recorded() -> None:
    risk, broker, accounts = manager()

    outcome = await risk.submit(BOT, order(amount="9.99"), REASON)

    # 25% of 1000 at 100: 2.5 BTC.
    assert outcome.decision.decision == "resized"
    assert outcome.decision.requested_amount == Decimal("9.99")
    ((_, placed, reason),) = broker.placed
    assert placed == order(amount="2.5")
    assert reason["risk"] == {
        "decision": "resized",
        "rule": "max_coin_exposure",
        "detail": outcome.decision.detail,
        "requested_amount": "9.99",
    }
    assert outcome.order is not None and outcome.order.amount == Decimal("2.5")
    assert [e.decision for e in accounts.events] == [outcome.decision]


@pytest.mark.parametrize("cid", ["b1-202610231300-s", "b1-202610231200-x1"])
async def test_a_sell_or_a_stop_exit_passes_with_every_limit_breached(cid: str) -> None:
    worst = Account(
        equity=Decimal(500),
        peak=Decimal(1000),
        day_start=Decimal(1000),
        open_positions=9,
        coin_exposure=Decimal(900),
        daily_loss_hit=True,
    )
    risk, broker, accounts = manager(accounts=FakeAccounts(account=worst))

    with capture_logs() as logs:
        outcome = await risk.submit(BOT, order("sell", "0.25", cid=cid), REASON)

    assert outcome.decision == SELLS_ALLOWED
    assert broker.placed == [
        (
            BOT,
            order("sell", "0.25", cid=cid),
            {**REASON, "risk": SELLS_ALLOWED.model_dump(mode="json", exclude_none=True)},
        )
    ]
    assert (accounts.snapshots, accounts.events) == (0, [])  # not even looked at
    assert [(log["event"], log["rule"]) for log in logs] == [("risk.decision", "reduces_exposure")]
    assert "account" not in logs[0]


async def test_a_retried_order_gets_its_first_answer() -> None:
    risk, _, accounts = manager()
    first = await risk.submit(BOT, order(), REASON)
    # The account has changed since: deciding again would block it.
    accounts.account = replace(FLAT, open_positions=3)

    with capture_logs() as logs:
        again = await risk.submit(BOT, order(), REASON)

    assert again == first
    assert accounts.events == []
    assert [log["event"] for log in logs] == ["risk.repeat"]


async def test_a_retried_resized_order_returns_the_smaller_order() -> None:
    risk, _, _ = manager()
    first = await risk.submit(BOT, order(amount="9.99"), REASON)

    again = await risk.submit(BOT, order(amount="9.99"), REASON)

    assert again == first
    assert again.order is not None and again.order.amount == Decimal("2.5")


async def test_a_retried_blocked_order_is_decided_again() -> None:
    # Nothing was placed, so there's no first answer to give.
    risk, _, accounts = manager(accounts=FakeAccounts(account=replace(FLAT, open_positions=3)))
    await risk.submit(BOT, order(), REASON)
    accounts.account = FLAT

    again = await risk.submit(BOT, order(), REASON)

    assert again.decision == WITHIN_LIMITS and again.order is not None
    assert len(accounts.events) == 1


async def test_a_different_order_under_a_used_id_is_refused() -> None:
    risk, _, _ = manager()
    await risk.submit(BOT, order(), REASON)

    with pytest.raises(DuplicateClientOrderId):
        await risk.submit(BOT, order(amount="0.7"), REASON)


async def test_two_buys_at_once_cannot_both_take_the_last_slot() -> None:
    @dataclass
    class Counting(FakeAccounts):
        """Counts what the broker placed, and yields halfway, as a database read would."""

        broker: FakeBroker = field(default_factory=FakeBroker)

        async def snapshot(self, symbol: str) -> Account:
            placed = len(self.broker.placed)
            await asyncio.sleep(0)
            return replace(FLAT, open_positions=placed)

    broker = FakeBroker()
    accounts = Counting(settings_=RiskSettings(max_open_positions=1), broker=broker)
    risk = RiskManager(broker, accounts, Rules())

    outcomes = await asyncio.gather(
        risk.submit(BOT, order(cid="b1-202610231200-b"), REASON),
        risk.submit(2, order(cid="b2-202610231200-b"), REASON),
    )

    assert [o.decision.decision for o in outcomes] == ["allowed", "blocked"]
    assert len(broker.placed) == 1


async def test_a_broker_error_passes_up_after_the_decision_is_logged() -> None:
    clash = DuplicateClientOrderId("already used by a different order")
    risk, _, _ = manager(FakeBroker(error=clash))

    with capture_logs() as logs, pytest.raises(DuplicateClientOrderId) as raised:
        await risk.submit(BOT, order(), REASON)

    assert raised.value is clash
    assert [log["event"] for log in logs] == ["risk.decision"]


async def test_a_reason_cannot_bring_its_own_risk_check() -> None:
    risk, broker, _ = manager()

    with capture_logs() as logs, pytest.raises(ValueError, match="risk"):
        await risk.submit(BOT, order(), {**REASON, "risk": {"decision": "allowed"}})

    assert (broker.placed, logs) == ([], [])


async def test_the_accounts_must_be_the_brokers_mode() -> None:
    with pytest.raises(ValueError, match="live"):
        RiskManager(FakeBroker(), FakeAccounts(mode="live"), Rules())


async def test_a_cancel_is_never_blocked_and_is_logged() -> None:
    risk, broker, accounts = manager()
    await risk.submit(BOT, order(), REASON)
    accounts.account = replace(FLAT, daily_loss_hit=True)

    with capture_logs() as logs:
        canceled = await risk.cancel("b1-202610231200-b")

    assert broker.canceled == ["b1-202610231200-b"]
    assert canceled.status == "canceled"
    assert logs == [
        {
            "event": "risk.cancel",
            "log_level": "info",
            "mode": "paper",
            "client_order_id": "b1-202610231200-b",
        }
    ]


async def test_reads_pass_through() -> None:
    risk, broker, _ = manager()
    placed = (await risk.submit(BOT, order(), REASON)).order

    assert risk.mode == "paper"
    assert await risk.get_order("b1-202610231200-b") == placed
    assert await risk.get_order("b1-nothing") is None
    assert await risk.balances(BOT) == await broker.balances(BOT)
    assert await risk.position(BOT, BTC) == Position(amount=Decimal("0.5"), avg_entry=Decimal(100))


# --- With a real PaperBroker and PaperAccounts ----------------------------------------


@dataclass
class FreshQuote:
    async def latest(self, exchange: str, symbol: str) -> PriceUpdate | None:
        return PriceUpdate.model_validate(
            {
                "exchange": exchange,
                "symbol": symbol,
                "bid": "99.9",
                "ask": "100",
                "bid_size": "5",
                "ask_size": "5",
                "ts": T0,
                "received_at": T0,
                "source": "ws",
            }
        )


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE orders, fills, positions, paper_balances, risk_events"))
    await insert_bot(engine, BOT)
    await set_limits(engine)
    yield engine
    await engine.dispose()


async def paper(db: AsyncEngine) -> RiskManager:
    broker = PaperBroker(db, FreshQuote(), Rules(), clock=lambda: T0)
    await broker.deposit(BOT, "USDT", Decimal(1000))
    return RiskManager(broker, PaperAccounts(db, FreshQuote(), clock=lambda: T0), Rules())


async def test_a_paper_order_is_stored_with_its_risk_check(db: AsyncEngine) -> None:
    risk = await paper(db)

    first = await risk.submit(BOT, order(), REASON)
    again = await risk.submit(BOT, order(), REASON)  # a retry after a timeout

    assert first.order is not None and again.order is not None
    assert first.order.reason == {**REASON, "risk": WITHIN}
    assert first.order.status == "closed"  # crossed the 100 ask with 5 on offer
    assert again.order == first.order
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(OrderRow))).scalar_one() == 1


async def test_a_paper_block_is_written_to_risk_events(db: AsyncEngine) -> None:
    risk = await paper(db)
    await risk.submit(BOT, order(), REASON)  # opens a position
    await set_limits(db, max_open_positions=1)

    blocked = await risk.submit(BOT, order(cid="b1-202610231201-b"), REASON)

    assert blocked.order is None
    async with db.connect() as conn:
        rows = (await conn.execute(select(RiskEventRow))).all()
    assert [(r.bot_id, r.client_order_id, r.rule, r.decision) for r in rows] == [
        (BOT, "b1-202610231201-b", "max_open_positions", "blocked")
    ]
    assert rows[0].inputs["account"]["open_positions"] == 1
    assert rows[0].inputs["settings"]["max_open_positions"] == 1
    assert await risk.get_order("b1-202610231201-b") is None  # never placed
