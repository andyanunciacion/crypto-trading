"""Which log lines are alerts, and how repeats are held back (shared/alerts.py, D59)."""

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shared.alerts import ALERT_EVENTS, EVENTS, Alert, Passed, Quieter, to_alert
from shared.db import models
from shared.logs import LogEntry, mirrored_line, read_entry

T0 = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)


def line(event: str, level: str = "info", service: str = "engine", **fields: Any) -> LogEntry:
    """`event` logged by `service`, read back from the stream as the alerts service reads it."""
    raw = mirrored_line(
        {"event": event, "level": level, "timestamp": T0.isoformat(), **fields}, service
    )
    entry = read_entry("1-0", raw.encode())
    assert entry is not None
    return entry


def event_of(entry: LogEntry) -> str | None:
    alert = to_alert(entry)
    return None if alert is None else alert.event


def test_every_event_has_its_label_and_default_and_matches_the_database_check() -> None:
    assert tuple(EVENTS) == ALERT_EVENTS == models.ALERT_EVENTS
    assert [e for e, info in EVENTS.items() if not info.default] == ["risk_resize"]


def test_the_migrations_seed_each_events_default() -> None:
    seeded: dict[str, bool] = {}
    for name in ("0017_alert_settings", "0018_engine_down_alert"):
        seeded |= importlib.import_module(f"shared.db.migrations.versions.{name}").DEFAULTS
    assert {event: info.default for event, info in EVENTS.items()} == seeded


# --- Trades ------------------------------------------------------------------------------


def test_an_order_is_one_trade_when_its_last_slice_fills() -> None:
    sliced: dict[str, Any] = {
        "bot_id": 3,
        "client_order_id": "b3-x-b",
        "symbol": "BTC/USDT",
        "side": "buy",
    }

    assert event_of(line("broker.order.filled", status="open", filled="0.1", **sliced)) is None
    assert event_of(line("broker.order.filled", status="closed", filled="0.3", **sliced)) == "trade"


def test_an_order_cancelled_after_part_filled_is_a_trade_and_one_with_none_isnt() -> None:
    order: dict[str, Any] = {"bot_id": 3, "client_order_id": "b3-x-b", "symbol": "BTC/USDT"}

    assert event_of(line("broker.order.canceled", filled="0.0005", **order)) == "trade"
    assert event_of(line("broker.order.canceled", filled="0", **order)) is None
    assert event_of(line("broker.order.canceled", filled="0E-8", **order)) is None


# --- Stops, risk, switches, the kill switch ----------------------------------------------


def test_a_stop_is_an_alert_the_first_time_it_fires_not_for_each_exit_after() -> None:
    assert event_of(line("bot.exit", bot_id=3, kind="stop_loss", again=False)) == "stop"
    assert event_of(line("bot.exit", bot_id=3, kind="stop_loss", again=True)) is None


def test_a_blocked_buy_and_a_shrunk_one_are_alerts_an_allowed_one_isnt() -> None:
    def decision(value: str) -> LogEntry:
        return line("risk.decision", bot_id=3, side="buy", decision=value, rule="max_drawdown")

    assert event_of(decision("blocked")) == "risk_block"
    assert event_of(decision("resized")) == "risk_resize"
    assert event_of(decision("allowed")) is None


@pytest.mark.parametrize(
    ("event", "level", "expected"),
    [
        ("risk.breaker.tripped", "warning", "breaker"),
        ("bot.switch", "info", "switch"),
        ("kill_switch.done", "warning", "kill_switch"),
    ],
)
def test_breakers_switches_and_the_kill_switch_are_alerts(
    event: str, level: str, expected: str
) -> None:
    assert event_of(line(event, level, bot_id=3)) == expected


# --- Errors ------------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["engine", "backtester", "api"])
def test_an_error_from_any_service_is_an_alert(service: str) -> None:
    assert event_of(line("bot.error", "error", service, bot_id=3)) == "error"
    assert event_of(line("anything.failed", "critical", service)) == "error"


def test_warnings_and_info_lines_are_not_alerts() -> None:
    assert event_of(line("prices.stream.reconnecting", "warning")) is None
    assert event_of(line("bot.order", "info", bot_id=3)) is None


def test_the_alerts_services_own_lines_never_are_or_a_failed_send_would_loop() -> None:
    assert event_of(line("alerts.send_failed", "error", "alerts")) is None
    assert event_of(line("kill_switch.done", "warning", "alerts")) is None


# --- Holding back repeats ----------------------------------------------------------------


def blocked(bot_id: int = 3, rule: str = "daily_loss") -> Alert:
    alert = to_alert(line("risk.decision", bot_id=bot_id, decision="blocked", rule=rule))
    assert alert is not None
    return alert


def error(event: str = "bot.error", bot_id: int | None = 3, service: str = "engine") -> Alert:
    fields = {} if bot_id is None else {"bot_id": bot_id}
    alert = to_alert(line(event, "error", service, **fields))
    assert alert is not None
    return alert


def test_a_block_repeating_within_the_hour_is_held_back_and_counted_in_the_next() -> None:
    quiet = Quieter()

    assert quiet.check(blocked(), T0) == Passed()
    assert quiet.check(blocked(), T0 + timedelta(minutes=1)) is None
    assert quiet.check(blocked(), T0 + timedelta(minutes=59)) is None
    # An hour after the one sent, the next goes, saying what was held back since when.
    assert quiet.check(blocked(), T0 + timedelta(hours=1)) == Passed(
        held=2, since=T0 + timedelta(minutes=1)
    )
    assert quiet.check(blocked(), T0 + timedelta(hours=1, minutes=1)) is None


def test_blocks_by_another_rule_or_bot_are_not_held_back_by_this_one() -> None:
    quiet = Quieter()
    quiet.check(blocked(3, "daily_loss"), T0)

    assert quiet.check(blocked(3, "stale_data"), T0) == Passed()
    assert quiet.check(blocked(4, "daily_loss"), T0) == Passed()


def test_the_same_error_is_held_back_for_ten_minutes_another_isnt() -> None:
    quiet = Quieter()
    quiet.check(error(), T0)

    assert quiet.check(error(), T0 + timedelta(minutes=9)) is None
    assert quiet.check(error("paper.feed.listener_error"), T0) == Passed()
    assert quiet.check(error(bot_id=4), T0) == Passed()
    assert quiet.check(error(service="api"), T0) == Passed()
    assert quiet.check(error(), T0 + timedelta(minutes=10)) == Passed(
        held=1, since=T0 + timedelta(minutes=9)
    )


def test_trades_are_never_held_back() -> None:
    quiet = Quieter()
    trade = to_alert(line("broker.order.filled", bot_id=3, status="closed"))
    assert trade is not None

    assert quiet.check(trade, T0) == Passed()
    assert quiet.check(trade, T0) == Passed()
