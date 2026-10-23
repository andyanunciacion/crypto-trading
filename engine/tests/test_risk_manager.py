"""The RiskManager stub: every order passes, logged with its inputs and stored with its decision.

Unit tests use a recording fake Broker; the last test puts a real PaperBroker (on a
throwaway TimescaleDB, see shared.testing) behind it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import JsonValue
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.brokers.base import Broker, BrokerOrder, DuplicateClientOrderId
from engine.brokers.paper import PaperBroker
from engine.exchange.models import Balance, MarketRules, OrderRequest
from engine.risk.manager import RiskDecision, RiskManager, RiskOutcome
from engine.strategies.base import Position
from shared.db.models import OrderRow
from shared.db.session import make_engine
from shared.prices import PriceUpdate
from shared.testing import insert_bot

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
BTC = "BTC/USDT"
BOT = 1
REASON: dict[str, JsonValue] = {"signal": {"action": "buy", "reason": "fast EMA crossed above"}}
PASS_THROUGH = RiskDecision(
    decision="allowed", rule="pass_through", detail="stub: every order is allowed until P4"
)
RISK: JsonValue = {"decision": "allowed", "rule": "pass_through", "detail": PASS_THROUGH.detail}


def order(side: str = "buy", amount: str = "0.5", cid: str = "b1-202610231200-b") -> OrderRequest:
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
    """Records every call; places nothing."""

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


async def test_an_order_passes_through_unchanged_with_its_decision() -> None:
    broker = FakeBroker()
    risk = RiskManager(broker)

    outcome = await risk.submit(BOT, order(), REASON)

    # The broker gets exactly the order asked for, and a reason that adds the risk check.
    assert broker.placed == [(BOT, order(), {**REASON, "risk": RISK})]
    placed = broker.orders[order().client_order_id]
    assert outcome == RiskOutcome(decision=PASS_THROUGH, order=placed)


async def test_every_order_is_logged_with_its_inputs() -> None:
    risk = RiskManager(FakeBroker())

    with capture_logs() as logs:
        await risk.submit(BOT, order("sell", "0.25", cid="b1-202610231300-s"), REASON)

    assert logs == [
        {
            "event": "risk.decision",
            "log_level": "info",
            "mode": "paper",
            "bot_id": BOT,
            "symbol": BTC,
            "side": "sell",
            "type": "limit",
            "amount": "0.25",
            "price": "100",
            "client_order_id": "b1-202610231300-s",
            "reason": REASON,
            "decision": "allowed",
            "rule": "pass_through",
            "detail": PASS_THROUGH.detail,
        }
    ]


async def test_a_broker_error_passes_up_after_the_decision_is_logged() -> None:
    clash = DuplicateClientOrderId("already used by a different order")
    risk = RiskManager(FakeBroker(error=clash))

    with capture_logs() as logs, pytest.raises(DuplicateClientOrderId) as raised:
        await risk.submit(BOT, order(), REASON)

    assert raised.value is clash
    assert [log["event"] for log in logs] == ["risk.decision"]


async def test_a_reason_cannot_bring_its_own_risk_check() -> None:
    broker = FakeBroker()
    risk = RiskManager(broker)

    with capture_logs() as logs, pytest.raises(ValueError, match="risk"):
        await risk.submit(BOT, order(), {**REASON, "risk": {"decision": "allowed"}})

    assert (broker.placed, logs) == ([], [])


async def test_a_cancel_is_never_blocked_and_is_logged() -> None:
    broker = FakeBroker()
    risk = RiskManager(broker)
    await risk.submit(BOT, order(), REASON)

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
    broker = FakeBroker()
    risk = RiskManager(broker)
    placed = (await risk.submit(BOT, order(), REASON)).order

    assert risk.mode == "paper"
    assert await risk.get_order("b1-202610231200-b") == placed
    assert await risk.get_order("b1-nothing") is None
    assert await risk.balances(BOT) == await broker.balances(BOT)
    assert await risk.position(BOT, BTC) == Position(amount=Decimal("0.5"), avg_entry=Decimal(100))


# --- With a real PaperBroker ---------------------------------------------------------


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


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE orders, fills, positions, paper_balances"))
    await insert_bot(engine, BOT)
    yield engine
    await engine.dispose()


async def test_a_paper_order_is_stored_with_its_risk_check(db: AsyncEngine) -> None:
    broker = PaperBroker(db, FreshQuote(), Rules(), clock=lambda: T0)
    await broker.deposit(BOT, "USDT", Decimal(1000))
    risk = RiskManager(broker)

    first = await risk.submit(BOT, order(), REASON)
    again = await risk.submit(BOT, order(), REASON)  # a retry after a timeout

    assert first.order is not None and again.order is not None
    assert first.order.reason == {**REASON, "risk": RISK}
    assert first.order.status == "closed"  # crossed the 100 ask with 5 on offer
    assert again.order == first.order
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(OrderRow))).scalar_one() == 1
