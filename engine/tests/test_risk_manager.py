"""The RiskManager: every order is decided on, logged with its inputs and stored with its
decision; blocks and resizes are recorded in `risk_events`, and sells are never blocked.
Circuit breakers block buys first, and failed orders pause a bot.

Unit tests use a recording fake Broker and fake accounts (the limits and breakers
themselves are tested in test_limits.py and test_breakers.py); the last tests put a real
PaperBroker and PaperAccounts (on a throwaway TimescaleDB, see shared.testing) behind it.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import JsonValue
from sqlalchemy import URL, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from engine.brokers.base import Broker, BrokerOrder, DuplicateClientOrderId, Mode
from engine.brokers.paper import PaperBroker
from engine.exchange.models import Balance, MarketRules, OrderRequest
from engine.risk.account import PaperAccounts
from engine.risk.breakers import Market, Swing
from engine.risk.limits import (
    SELLS_ALLOWED,
    WITHIN_LIMITS,
    Account,
    RiskEvent,
    RiskSettings,
)
from engine.risk.manager import RiskManager, RiskOutcome
from engine.strategies.base import Position
from shared.db.models import BotRow, OrderRow, RiskEventRow
from shared.db.session import make_engine
from shared.prices import PriceUpdate
from shared.testing import alert_of, insert_bot, set_limits

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
    "adds": False,
}
SETTINGS_JSON: JsonValue = {
    "max_open_positions": 3,
    "max_coin_exposure_pct": "25",
    "max_daily_loss_pct": "3",
    "max_drawdown_pct": "10",
    "stale_after_candles": None,
    "max_failed_orders": None,
    "failed_orders_minutes": None,
    "spike_pct": None,
    "spike_minutes": None,
}
# §6's circuit breakers, for the tests about them.
BREAKERS = DEFAULTS.model_copy(
    update={
        "stale_after_candles": 2,
        "max_failed_orders": 3,
        "failed_orders_minutes": 10,
        "spike_pct": Decimal(3),
        "spike_minutes": 5,
    }
)
CALM = Market(
    status="running",
    paused_reason=None,
    timeframe="1m",
    price_ages={BTC: timedelta(seconds=1)},
    swing=Swing(low=Decimal(100), high=Decimal(100)),
)
MARKET_JSON: JsonValue = {
    "status": "running",
    "paused_reason": None,
    "timeframe": "1m",
    "price_ages_s": {BTC: 1.0},
    "swing": {"low": "100", "high": "100"},
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
    raises if the request differs, as the PaperBroker does. With `reject`, every new
    order is stored as rejected."""

    mode = "paper"

    def __init__(self, error: Exception | None = None, *, reject: bool = False) -> None:
        self.error = error
        self.reject = reject
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
        placed = stored(bot_id, request, reason, "rejected" if self.reject else "open")
        if self.reject:
            placed = placed.model_copy(update={"reject_reason": "insufficient USDT"})
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
        return Position(amount=Decimal("0.5"), avg_entry=Decimal(100), last_entry=Decimal(100))


@dataclass
class FakeAccounts:
    settings_: RiskSettings = DEFAULTS
    account: Account = FLAT
    market_: Market = CALM
    mode: Mode = "paper"
    events: list[RiskEvent] = field(default_factory=list)
    paused: list[tuple[int, str]] = field(default_factory=list)
    snapshots: int = 0
    pause_error: Exception | None = None

    async def settings(self) -> RiskSettings:
        return self.settings_

    async def market(self, bot_id: int, symbol: str, settings: RiskSettings) -> Market:
        assert (symbol, settings) == (BTC, self.settings_)
        return self.market_

    async def snapshot(self, bot_id: int, symbol: str) -> Account:
        assert symbol == BTC
        self.snapshots += 1
        return self.account

    async def record(self, event: RiskEvent) -> None:
        self.events.append(event)

    async def pause(self, bot_id: int, reason: str) -> bool:
        """As `bots` would: paused from now on, so its next buy finds it paused."""
        if self.pause_error is not None:
            raise self.pause_error
        self.paused.append((bot_id, reason))
        self.market_ = replace(self.market_, status="paused", paused_reason=reason)
        return True


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


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


def manager(
    broker: Broker | None = None, accounts: FakeAccounts | None = None, clock: Clock | None = None
) -> tuple[RiskManager, FakeBroker, FakeAccounts]:
    broker = broker or FakeBroker()
    assert isinstance(broker, FakeBroker)
    accounts = accounts or FakeAccounts()
    return RiskManager(broker, accounts, Rules(), clock=clock or Clock()), broker, accounts


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
            "market": MARKET_JSON,
            "account": ACCOUNT_JSON,
        }
    ]
    assert alert_of(logs[0]) is None  # an allowed buy isn't an alert (D59)


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
                "market": MARKET_JSON,
                "account": account.to_json(),
            },
        )
    ]
    assert [(log["event"], log["decision"], log["rule"]) for log in logs] == [
        ("risk.decision", "blocked", rule)
    ]
    assert alert_of(logs[0]) == "risk_block"  # D59


# --- Circuit breakers (D42) -------------------------------------------------------------

TRIPPED = [
    (replace(CALM, status="paused", paused_reason="by hand"), "bot_paused"),
    (replace(CALM, status="stopped"), "bot_stopped"),
    (replace(CALM, price_ages={BTC: timedelta(minutes=3)}), "stale_data"),  # over 2 candles
    (replace(CALM, swing=Swing(low=Decimal(100), high=Decimal(104))), "price_spike"),  # 4%
]


@pytest.mark.parametrize(("market", "rule"), TRIPPED)
async def test_each_breaker_pauses_buys_and_records_why(market: Market, rule: str) -> None:
    accounts = FakeAccounts(settings_=BREAKERS, market_=market)
    risk, broker, _ = manager(accounts=accounts)

    with capture_logs() as logs:
        outcome = await risk.submit(BOT, order(), REASON)

    assert outcome.order is None
    assert (outcome.decision.decision, outcome.decision.rule) == ("blocked", rule)
    assert broker.placed == []
    (event,) = accounts.events
    assert (event.client_order_id, event.decision) == ("b1-202610231200-b", outcome.decision)
    assert event.inputs["market"] == market.to_json()
    (logged,) = logs
    assert (logged["event"], logged["rule"], logged["detail"]) == (
        "risk.decision",
        rule,
        outcome.decision.detail,
    )
    assert alert_of(logged) == "risk_block"  # D59


async def test_a_tripped_breaker_decides_before_the_limits() -> None:
    # Over every limit too, but a stale price means the account can't be valued at all.
    worst = replace(FLAT, equity=Decimal(500), open_positions=9, daily_loss_hit=True)
    stale = replace(CALM, price_ages={BTC: None})
    risk, _, _ = manager(accounts=FakeAccounts(settings_=BREAKERS, account=worst, market_=stale))

    outcome = await risk.submit(BOT, order(), REASON)

    assert outcome.decision.rule == "stale_data"


@pytest.mark.parametrize("market", [market for market, _ in TRIPPED])
async def test_a_sell_passes_every_tripped_breaker(market: Market) -> None:
    risk, broker, _ = manager(accounts=FakeAccounts(settings_=BREAKERS, market_=market))

    outcome = await risk.submit(BOT, order("sell", cid="b1-202610231200-x1"), REASON)

    assert outcome.decision == SELLS_ALLOWED
    assert len(broker.placed) == 1


async def test_three_failed_orders_in_ten_minutes_pause_the_bot() -> None:
    clock = Clock()
    accounts = FakeAccounts(settings_=BREAKERS)
    risk, broker, _ = manager(FakeBroker(reject=True), accounts, clock)

    with capture_logs() as logs:
        for i in range(3):
            outcome = await risk.submit(BOT, order(cid=f"b1-20261023120{i}-b"), REASON)
            assert outcome.order is not None and outcome.order.status == "rejected"
            clock.now += timedelta(minutes=5)  # the third comes 10 minutes after the first

    reason = (
        "3 failed orders within 10 min: b1-202610231200-b (rejected: insufficient USDT),"
        " b1-202610231201-b (rejected: insufficient USDT),"
        " b1-202610231202-b (rejected: insufficient USDT)"
    )
    assert accounts.paused == [(BOT, reason)]
    (tripped,) = [log for log in logs if log["event"] == "risk.breaker.tripped"]
    assert alert_of(tripped) == "breaker"  # D59
    assert tripped == {
        "event": "risk.breaker.tripped",
        "log_level": "warning",
        "mode": "paper",
        "bot_id": BOT,
        "client_order_id": "b1-202610231202-b",
        "rule": "repeated_failures",
        "paused": True,
        "detail": reason,
        "failures": [
            {
                "ts": (T0 + timedelta(minutes=5 * i)).isoformat(),
                "client_order_id": f"b1-20261023120{i}-b",
                "why": "rejected: insufficient USDT",
            }
            for i in range(3)
        ],
    }

    # Paused: its next buy is blocked, saying why; a sell still goes through.
    broker.reject = False
    blocked = await risk.submit(BOT, order(cid="b1-202610231215-b"), REASON)
    assert (blocked.decision.rule, blocked.decision.detail) == (
        "bot_paused",
        f"the bot is paused: {reason}",
    )
    sold = await risk.submit(BOT, order("sell", cid="b1-202610231215-s"), REASON)
    assert sold.order is not None

    # Set running again (by hand until P5): it buys again.
    accounts.market_ = CALM
    resumed = await risk.submit(BOT, order(cid="b1-202610231216-b"), REASON)
    assert resumed.decision == WITHIN_LIMITS


async def test_a_raising_broker_counts_and_its_error_still_passes_up() -> None:
    clash = DuplicateClientOrderId("already used by a different order")
    accounts = FakeAccounts(settings_=BREAKERS)
    risk, _, _ = manager(FakeBroker(error=clash), accounts)

    for i in range(3):
        with pytest.raises(DuplicateClientOrderId) as raised:
            await risk.submit(BOT, order(cid=f"b1-20261023120{i}-b"), REASON)
        assert raised.value is clash

    ((_, reason),) = accounts.paused
    assert reason.endswith(
        "b1-202610231202-b (DuplicateClientOrderId: already used by a different order)"
    )


async def test_a_failed_pause_is_logged_and_the_orders_answer_still_returns() -> None:
    accounts = FakeAccounts(settings_=BREAKERS, pause_error=RuntimeError("database down"))
    risk, _, _ = manager(FakeBroker(reject=True), accounts)

    with capture_logs() as logs:
        for i in range(3):
            outcome = await risk.submit(BOT, order(cid=f"b1-20261023120{i}-b"), REASON)
            assert outcome.order is not None and outcome.order.status == "rejected"

    (error,) = [log for log in logs if log["event"] == "risk.breaker.error"]
    assert (error["client_order_id"], error["error"], error["detail"]) == (
        "b1-202610231202-b",
        "RuntimeError",
        "database down",
    )


async def test_a_retried_rejected_order_counts_once() -> None:
    accounts = FakeAccounts(settings_=BREAKERS)
    risk, _, _ = manager(FakeBroker(reject=True), accounts)

    for _ in range(3):
        await risk.submit(BOT, order(), REASON)  # its first answer, every time

    assert accounts.paused == []


async def test_failures_with_the_error_breaker_off_pause_nothing() -> None:
    accounts = FakeAccounts()  # DEFAULTS: limits only
    risk, _, _ = manager(FakeBroker(reject=True), accounts)

    for i in range(5):
        await risk.submit(BOT, order(cid=f"b1-20261023120{i}-b"), REASON)

    assert accounts.paused == []


async def test_a_buy_over_the_coin_cap_is_placed_smaller_and_recorded() -> None:
    risk, broker, accounts = manager()

    with capture_logs() as logs:
        outcome = await risk.submit(BOT, order(amount="9.99"), REASON)

    # 25% of 1000 at 100: 2.5 BTC.
    assert outcome.decision.decision == "resized"
    assert [alert_of(log) for log in logs] == ["risk_resize"]  # D59
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

        async def snapshot(self, bot_id: int, symbol: str) -> Account:
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
    assert await risk.position(BOT, BTC) == Position(
        amount=Decimal("0.5"), avg_entry=Decimal(100), last_entry=Decimal(100)
    )


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

    # A second coin would be a second position.
    eth = order(cid="b1-202610231201-b").model_copy(update={"symbol": "ETH/USDT"})
    blocked = await risk.submit(BOT, eth, REASON)

    assert blocked.order is None
    async with db.connect() as conn:
        rows = (await conn.execute(select(RiskEventRow))).all()
    assert [(r.bot_id, r.client_order_id, r.rule, r.decision) for r in rows] == [
        (BOT, "b1-202610231201-b", "max_open_positions", "blocked")
    ]
    assert rows[0].inputs["account"]["open_positions"] == 1
    assert rows[0].inputs["settings"]["max_open_positions"] == 1
    assert await risk.get_order("b1-202610231201-b") is None  # never placed


async def test_a_paper_buy_that_adds_to_the_bots_position_opens_none(db: AsyncEngine) -> None:
    # Grid and DCA (P6-6) buy more of a coin they hold: still one position.
    risk = await paper(db)
    await risk.submit(BOT, order(), REASON)
    await set_limits(db, max_open_positions=1)

    added = await risk.submit(BOT, order(cid="b1-202610231201-b"), REASON)

    assert added.order is not None and added.order.reason["risk"] == WITHIN
    assert await risk.position(BOT, BTC) == Position(
        amount=Decimal(1), avg_entry=Decimal(100), last_entry=Decimal(100)
    )


async def risk_events(db: AsyncEngine) -> list[tuple[str, str, str]]:
    async with db.connect() as conn:
        rows = (await conn.execute(select(RiskEventRow).order_by(RiskEventRow.id))).all()
    return [(r.client_order_id, r.rule, r.detail) for r in rows]


async def test_a_paper_bot_paused_by_failed_orders_stays_paused_until_set_running(
    db: AsyncEngine,
) -> None:
    await set_limits(db, max_failed_orders=3, failed_orders_minutes=10)
    risk = await paper(db)
    for i in range(3):
        # 20 BTC at 100 is 2000 USDT, with 1000 there: rejected.
        rejected = await risk.submit(BOT, order(amount="20", cid=f"b1-20261023120{i}-b"), REASON)
        assert rejected.order is not None and rejected.order.status == "rejected"

    async with db.connect() as conn:
        bot = (
            await conn.execute(
                select(BotRow.status, BotRow.paused_reason, BotRow.paused_at).where(
                    BotRow.id == BOT
                )
            )
        ).one()
    assert (bot.status, bot.paused_at) == ("paused", T0)
    assert bot.paused_reason.startswith(
        "3 failed orders within 10 min: b1-202610231200-b (rejected: "
    )

    # Read from `bots` on every buy, so a restarted engine finds it paused too.
    restarted = RiskManager(
        PaperBroker(db, FreshQuote(), Rules(), clock=lambda: T0),
        PaperAccounts(db, FreshQuote(), clock=lambda: T0),
        Rules(),
        clock=lambda: T0,
    )
    blocked = await restarted.submit(BOT, order(cid="b1-202610231203-b"), REASON)
    assert blocked.order is None
    assert await risk_events(db) == [
        ("b1-202610231203-b", "bot_paused", f"the bot is paused: {bot.paused_reason}")
    ]

    # Set running again by hand (P5 adds a button): the next buy goes through.
    async with db.begin() as conn:
        await conn.execute(
            update(BotRow)
            .where(BotRow.id == BOT)
            .values(status="running", paused_reason=None, paused_at=None)
        )
    resumed = await restarted.submit(BOT, order(cid="b1-202610231204-b"), REASON)
    assert resumed.decision == WITHIN_LIMITS and resumed.order is not None


async def test_a_paper_buy_on_a_stale_price_is_blocked_and_recorded(db: AsyncEngine) -> None:
    await set_limits(db, stale_after_candles=2)
    later = T0 + timedelta(minutes=3)  # the only price is from T0: over 2 of bot 1's 1m candles
    risk = RiskManager(
        PaperBroker(db, FreshQuote(), Rules(), clock=lambda: later),
        PaperAccounts(db, FreshQuote(), clock=lambda: later),
        Rules(),
        clock=lambda: later,
    )

    blocked = await risk.submit(BOT, order(), REASON)

    assert blocked.order is None
    assert await risk_events(db) == [
        (
            "b1-202610231200-b",
            "stale_data",
            "the price for BTC/USDT is 180 s old, over 2 candles of 1m (120 s):"
            " its feed may be down",
        )
    ]
