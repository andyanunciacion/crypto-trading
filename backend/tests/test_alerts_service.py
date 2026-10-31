"""The alerts service (backend/alerts/service.py, D59) on the test database and Redis.

Lines are written to the `logs` stream by a real `LogMirror`, as the engine writes
them, and a fake sender stands in for Telegram.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import URL, insert, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from backend.alerts.service import CURSOR_KEY, SUMMARY_KEY, AlertsService
from backend.alerts.status import read_status
from shared.alerts import EVENTS
from shared.db.models import (
    AlertSettingRow,
    BotRow,
    FillRow,
    OrderRow,
    PositionRow,
    RiskEventRow,
    StrategyConfigRow,
    StrategySwitchRow,
)
from shared.db.session import make_engine
from shared.logs import LogMirror

T0 = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)  # 10:00 in Manila
YESTERDAY = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)  # noon in Manila, the day before
BTC = "BTC/USDT"


@dataclass
class FakeSender:
    configured: bool = True
    last_sent_at: datetime | None = None
    last_error: str | None = None
    sent: list[str] = field(default_factory=list)

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        self.last_sent_at = T0
        return True

    async def close(self) -> None:
        return None


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Setup:
    db: AsyncEngine
    redis: Redis
    sender: FakeSender
    clock: Clock
    mirror: LogMirror

    def service(self) -> AlertsService:
        async def no_sleep(seconds: float) -> None:
            return None

        return AlertsService(
            self.redis,
            self.db,
            self.sender,
            mode="paper",
            clock=self.clock,
            sleep=no_sleep,
            trade_read_pause=0,
        )

    async def log(self, event: str, level: str = "info", **fields: Any) -> None:
        """Log a line as the engine would, and send it to the stream."""
        self.mirror(
            None, level, {"event": event, "level": level, "timestamp": T0.isoformat(), **fields}
        )
        await self.mirror.flush(self.redis)


@pytest.fixture
async def setup(db_url: URL, redis_url: str) -> AsyncIterator[Setup]:
    db = make_engine(db_url)
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE bots, strategy_configs, orders, fills, positions, paper_balances,"
                " risk_events, strategy_switches RESTART IDENTITY CASCADE"
            )
        )
        for event, info in EVENTS.items():
            await conn.execute(
                update(AlertSettingRow)
                .where(AlertSettingRow.event == event)
                .values(enabled=info.default)
            )
        await conn.execute(
            insert(BotRow).values(
                name="btc-dca", exchange="mexc", symbol=BTC, timeframe="1m", status="running"
            )
        )
    try:
        yield Setup(db, redis, FakeSender(), Clock(), LogMirror("engine"))
    finally:
        await redis.aclose()
        await db.dispose()


async def started(setup: Setup) -> AlertsService:
    """A service that has read where the stream ends."""
    service = setup.service()
    await service.poll(block_ms=1)
    return service


async def test_lines_logged_after_it_starts_become_messages_and_older_ones_dont(
    setup: Setup,
) -> None:
    await setup.log("bot.error", "error", bot_id=1, error="KeyError", detail="old")
    service = await started(setup)

    await setup.log("bot.error", "error", bot_id=1, error="KeyError", detail="new")
    await setup.log("bot.order", bot_id=1)  # not an alert
    sent = await service.poll(block_ms=1)

    assert sent == 1
    assert setup.sender.sent[0].startswith("Error in engine: bot.error (btc-dca)\nKeyError: new")


async def test_a_restart_carries_on_from_the_cursor_without_repeats(setup: Setup) -> None:
    service = await started(setup)
    await setup.log("bot.error", "error", bot_id=1, detail="first")
    await service.poll(block_ms=1)

    await setup.log("bot.error", "error", bot_id=1, detail="while it was down")
    restarted = setup.service()  # its quieter is new, so the repeat goes
    await restarted.poll(block_ms=1)

    assert [m.splitlines()[1] for m in setup.sender.sent] == ["first", "while it was down"]
    assert await setup.redis.get(CURSOR_KEY) == restarted.cursor.encode()  # type: ignore[union-attr]


async def test_a_turned_off_event_sends_nothing_and_turning_it_on_applies_at_once(
    setup: Setup,
) -> None:
    service = await started(setup)
    async with setup.db.begin() as conn:
        await conn.execute(
            update(AlertSettingRow).where(AlertSettingRow.event == "error").values(enabled=False)
        )
    await setup.log("bot.error", "error", bot_id=1, detail="muted")
    await service.poll(block_ms=1)
    assert setup.sender.sent == []

    async with setup.db.begin() as conn:
        await conn.execute(
            update(AlertSettingRow).where(AlertSettingRow.event == "error").values(enabled=True)
        )
    await setup.log("bot.error", "error", bot_id=1, detail="heard")
    await service.poll(block_ms=1)
    assert len(setup.sender.sent) == 1


async def test_shrunk_buys_are_off_at_first(setup: Setup) -> None:
    service = await started(setup)

    await setup.log("risk.decision", bot_id=1, decision="resized", rule="max_coin_exposure")

    assert await service.poll(block_ms=1) == 0


async def test_a_block_repeating_every_candle_sends_one_message_an_hour(setup: Setup) -> None:
    service = await started(setup)

    for _ in range(3):
        await setup.log(
            "risk.decision", bot_id=1, symbol=BTC, decision="blocked", rule="bot_paused"
        )
    await service.poll(block_ms=1)

    assert len(setup.sender.sent) == 1
    assert setup.sender.sent[0].startswith("Buy blocked: btc-dca · BTC/USDT\nbot_paused")


async def seed_trade(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await conn.execute(
            insert(OrderRow).values(
                bot_id=1,
                mode="paper",
                exchange="mexc",
                symbol=BTC,
                client_order_id="b1-202610310200-b",
                side="buy",
                type="limit",
                price=Decimal("64600.00"),
                amount=Decimal("0.0003"),
                filled=Decimal("0.0003"),
                status="closed",
                reason={
                    "signal": {
                        "action": "buy",
                        "ts": "2026-10-31T01:59:00Z",
                        "reason": "1 candles since the last buy",
                        "indicators": {},
                    },
                    "bot": {"id": 1, "name": "btc-dca"},
                    "strategy_config": {"id": 1, "strategy": "dca", "version": 2, "params": {}},
                },
            )
        )
        fill = {"order_id": 1, "fee_currency": "USDT"}
        await conn.execute(
            insert(FillRow),
            [
                {
                    **fill,
                    "ts": T0,
                    "price": Decimal("64500.10"),
                    "amount": Decimal("0.0001"),
                    "fee": Decimal("0.0064500"),
                },
                {
                    **fill,
                    "ts": T0 + timedelta(seconds=1),
                    "price": Decimal("64500.40"),
                    "amount": Decimal("0.0002"),
                    "fee": Decimal("0.0129001"),
                },
            ],
        )


async def test_a_trade_says_its_average_price_across_its_fills_and_why(setup: Setup) -> None:
    await seed_trade(setup.db)
    service = await started(setup)

    await setup.log(
        "broker.order.filled",
        bot_id=1,
        client_order_id="b1-202610310200-b",
        symbol=BTC,
        side="buy",
        price="64500.40",
        filled="0.0003",
        status="closed",
    )
    await service.poll(block_ms=1)

    assert setup.sender.sent[0].splitlines()[:4] == [
        "BUY btc-dca · BTC/USDT (paper)",
        "0.0003 BTC at 64,500.3 USDT on average",
        "Worth 19.35 USDT, fee 0.0193501 USDT",
        "Why: dca signal: 1 candles since the last buy",
    ]


async def test_a_trade_whose_rows_can_t_be_read_is_described_from_its_line(setup: Setup) -> None:
    service = await started(setup)

    await setup.log(
        "broker.order.filled",
        bot_id=1,
        client_order_id="b1-unknown-b",
        symbol=BTC,
        side="buy",
        price="64500.4",
        filled="0.0003",
        status="closed",
    )
    await service.poll(block_ms=1)

    assert "Order b1-unknown-b (details unavailable)" in setup.sender.sent[0]


async def test_with_the_database_down_an_error_still_goes_by_the_defaults(
    setup: Setup, db_url: URL
) -> None:
    down = make_engine(db_url.set(port=1))  # nothing listens there
    service = AlertsService(setup.redis, down, setup.sender, mode="paper", clock=setup.clock)
    try:
        await service.poll(block_ms=1)
        with capture_logs() as logs:
            await setup.log("bot.error", "error", bot_id=1, error="OSError", detail="db down")
            await service.poll(block_ms=1)
            await setup.log("kill_switch.done", "warning", ok=True, took_ms=5)
            await service.poll(block_ms=1)
    finally:
        await down.dispose()

    # No bot names either, so the bot is its id.
    assert setup.sender.sent[0].startswith("Error in engine: bot.error (bot 1)")
    assert len(setup.sender.sent) == 2
    # Said once for the outage, not on every read: on the real stack it was every 3 s.
    assert [line["event"] for line in logs].count("alerts.db_failed") == 1


async def test_a_kill_the_chat_got_as_a_reply_isn_t_sent_again_as_an_alert(setup: Setup) -> None:
    # The kills sent from Telegram whose reply the chat saw (backend/alerts/commands.py).
    service = AlertsService(
        setup.redis, setup.db, setup.sender, mode="paper", clock=setup.clock, shown_kills={"k-1"}
    )
    await service.poll(block_ms=1)

    await setup.log("kill_switch.done", "warning", id="k-1", ok=True, stopped=[1], took_ms=5)
    await setup.log("kill_switch.done", "warning", id="k-2", ok=True, stopped=[1], took_ms=7)
    await service.poll(block_ms=1)

    assert len(setup.sender.sent) == 1
    assert "in 7 ms" in setup.sender.sent[0]


async def test_each_message_updates_the_status_the_settings_page_shows(setup: Setup) -> None:
    service = await started(setup)

    await setup.log("bot.error", "error", bot_id=1)
    await service.poll(block_ms=1)

    status = await read_status(setup.redis)
    assert status is not None and status.configured and status.last_sent_at == T0


# --- The daily summary -------------------------------------------------------------------


async def seed_yesterday(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await conn.execute(
            insert(StrategyConfigRow),
            [
                {"bot_id": 1, "strategy": "dca", "version": 2, "params": {}, "created_by": "t"},
                {"bot_id": 1, "strategy": "grid", "version": 1, "params": {}, "created_by": "t"},
            ],
        )
        await conn.execute(
            insert(PositionRow).values(
                bot_id=1,
                mode="paper",
                exchange="mexc",
                symbol=BTC,
                side="long",
                amount=0,
                avg_entry=Decimal("64000"),
                fees_paid=Decimal("0.04"),
                opened_at=YESTERDAY - timedelta(hours=1),
                closed_at=YESTERDAY,
                realized_pnl=Decimal("1.25"),
            )
        )
        await conn.execute(
            insert(RiskEventRow),
            [
                {
                    "ts": YESTERDAY,
                    "mode": "paper",
                    "bot_id": 1,
                    "client_order_id": f"b1-{i}-b",
                    "rule": rule,
                    "decision": "blocked",
                    "detail": "",
                    "inputs": {},
                }
                for i, rule in enumerate(["bot_paused", "bot_paused", "stale_data"])
            ],
        )
        await conn.execute(
            insert(StrategySwitchRow).values(
                ts=YESTERDAY,
                bot_id=1,
                from_config=1,
                to_config=2,
                trigger="manual",
                policy="keep",
                reason="by hand",
                source="dashboard",
            )
        )


async def test_the_summary_goes_at_8_in_manila_once_about_the_day_before(setup: Setup) -> None:
    await seed_yesterday(setup.db)
    service = setup.service()
    setup.clock.now = datetime(2026, 10, 30, 23, 59, tzinfo=UTC)  # 07:59 in Manila

    assert not await service.summary_if_due()
    setup.clock.now = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)  # 08:00
    assert await service.summary_if_due()
    assert not await service.summary_if_due()

    lines = setup.sender.sent[0].splitlines()
    assert lines[0] == "Daily summary for Fri 30 Oct (Manila)"
    assert lines[2] == (
        "Yesterday: 1 trades closed, +1.25 USDT realized after fees; 0 orders filled; "
        "1 strategy switches"
    )
    assert lines[3] == "Buys blocked: bot_paused 2, stale_data 1"
    assert await setup.redis.get(SUMMARY_KEY) == b"2026-10-31"


async def test_a_restart_after_8_sends_a_missed_summary_once(setup: Setup) -> None:
    setup.clock.now = datetime(2026, 10, 31, 7, 0, tzinfo=UTC)  # 15:00 in Manila
    assert await setup.service().summary_if_due()

    assert not await setup.service().summary_if_due()  # restarted again the same day

    assert len(setup.sender.sent) == 1


async def test_a_summary_turned_off_isn_t_sent_but_counts_as_done(setup: Setup) -> None:
    async with setup.db.begin() as conn:
        await conn.execute(
            update(AlertSettingRow)
            .where(AlertSettingRow.event == "daily_summary")
            .values(enabled=False)
        )
    setup.clock.now = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)

    assert await setup.service().summary_if_due()

    assert setup.sender.sent == []
