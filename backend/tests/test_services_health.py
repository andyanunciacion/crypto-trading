"""Settings → System (P7-4, D62): every service's state from its beat in Redis and a live
check of the database and Redis; and an API route's unhandled exception reaching Sentry
without the request's body or the session cookie."""

from datetime import timedelta

import pytest
import structlog
from fastapi import Request
from pydantic import SecretStr

from backend.alerts.status import ALERTS_STATUS_KEY, AlertsStatus
from backend.health.report import Probe, Readings, ServiceHealth, judge
from backend.testing import T0, Api
from shared.errors import init_errors
from shared.heartbeat import (
    BACKTESTER_HEARTBEAT_KEY,
    ENGINE_HEARTBEAT_KEY,
    EngineHeartbeat,
    ServiceHeartbeat,
)
from shared.logs import configure_logging
from shared.testing import SentryEvents

UP = Probe(ok=True, ms=1.5)
DOWN = Probe(ok=False, error="ConnectionRefusedError")
STARTED = T0 - timedelta(hours=2)
BODY = "a-request-body-sentry-must-not-see"  # stands in for a password or a key


def engine_beat(age: float, bots: list[int] | None = None) -> EngineHeartbeat:
    return EngineHeartbeat(
        ts=T0 - timedelta(seconds=age),
        started_at=STARTED,
        mode="paper",
        beat=7,
        exchange="mexc",
        symbols=["BTC/USDT", "ETH/USDT"],
        bots=[1] if bots is None else bots,
    )


def readings(**changes: object) -> Readings:
    base: dict[str, object] = {
        "db": UP,
        "redis": UP,
        "engine": engine_beat(4),
        "backtester": ServiceHeartbeat(ts=T0 - timedelta(seconds=9), started_at=STARTED),
        "alerts": AlertsStatus(
            ts=T0 - timedelta(seconds=3), configured=True, last_sent_at=None, last_error=None
        ),
        "sentry": True,
    }
    return Readings(**{**base, **changes})  # type: ignore[arg-type]


def by_name(rows: list[ServiceHealth]) -> dict[str, ServiceHealth]:
    return {row.name: row for row in rows}


def test_all_up() -> None:
    rows = judge(T0, readings())

    assert [(r.name, r.state) for r in rows] == [
        ("api", "ok"),
        ("db", "ok"),
        ("redis", "ok"),
        ("engine", "ok"),
        ("backtester", "ok"),
        ("alerts", "ok"),
        ("sentry", "ok"),
    ]
    engine = by_name(rows)["engine"]
    assert (engine.age_s, engine.since) == (4.0, STARTED)
    assert engine.detail == "paper, 1 bot running, prices for BTC/USDT, ETH/USDT"
    assert by_name(rows)["db"].detail == "answered in 1.5 ms"


def test_a_beat_past_its_cutoff_is_stale_and_a_missing_one_unknown() -> None:
    rows = by_name(
        judge(T0, readings(engine=engine_beat(31, bots=[]), backtester=None, alerts=None))
    )

    assert (rows["engine"].state, rows["engine"].age_s) == ("stale", 31.0)
    assert rows["engine"].detail == "no heartbeat for 31 s: down or stuck"
    assert rows["backtester"].state == "unknown"
    assert rows["backtester"].detail == "no heartbeat yet: has it run?"
    assert rows["alerts"].state == "unknown"


def test_the_cutoffs_are_inclusive_and_long_ages_read_in_minutes() -> None:
    rows = by_name(judge(T0, readings(engine=engine_beat(30))))
    assert rows["engine"].state == "ok"  # 30 s exactly, as Docker's check

    rows = by_name(judge(T0, readings(engine=engine_beat(600))))
    assert rows["engine"].detail == "no heartbeat for 10 min: down or stuck"


def test_with_redis_down_no_beat_can_be_judged() -> None:
    rows = by_name(judge(T0, readings(redis=DOWN)))

    assert rows["redis"].state == "down"
    assert rows["redis"].detail == "no answer (ConnectionRefusedError)"
    assert {rows[n].state for n in ("engine", "backtester", "alerts")} == {"unknown"}


def test_the_database_down_and_sentry_off() -> None:
    rows = by_name(judge(T0, readings(db=DOWN, sentry=False)))

    assert rows["db"].state == "down"
    assert (rows["sentry"].state, rows["sentry"].detail) == (
        "off",
        "SENTRY_DSN isn't set: errors stay in the logs",
    )


@pytest.mark.parametrize(
    ("configured", "last_error", "detail"),
    [
        (False, None, "Telegram isn't set up: alerts are only logged"),
        (
            True,
            "Forbidden: bot was blocked",
            "the last message didn't go: Forbidden: bot was blocked",
        ),
        (True, None, "sending to Telegram"),
    ],
)
def test_the_alerts_detail(configured: bool, last_error: str | None, detail: str) -> None:
    status = AlertsStatus(ts=T0, configured=configured, last_sent_at=None, last_error=last_error)

    assert by_name(judge(T0, readings(alerts=status)))["alerts"].detail == detail


async def test_the_route_reads_every_beat(api: Api) -> None:
    await api.redis.set(ENGINE_HEARTBEAT_KEY, engine_beat(5).model_dump_json())
    stale = ServiceHeartbeat(ts=T0 - timedelta(seconds=45), started_at=STARTED)
    await api.redis.set(BACKTESTER_HEARTBEAT_KEY, stale.model_dump_json())
    await api.redis.set(ALERTS_STATUS_KEY, b"not json")  # unreadable reads as missing
    await api.login()

    response = await api.client.get("/health/services")

    assert response.status_code == 200
    body = response.json()
    assert body["checked_at"] == "2026-10-26T12:00:05Z"
    states = {row["name"]: row["state"] for row in body["services"]}
    assert states == {
        "api": "ok",
        "db": "ok",
        "redis": "ok",
        "engine": "ok",
        "backtester": "stale",
        "alerts": "unknown",
        "sentry": "off",
    }


async def test_an_unhandled_route_exception_reaches_sentry_without_the_body_or_cookie(
    api: Api, sentry_events: SentryEvents
) -> None:
    settings = api.settings.model_copy(
        update={"sentry_dsn": SecretStr("https://abc@o1.ingest.sentry.io/1")}
    )
    errors = init_errors(settings, "api", transport=sentry_events, environ={})
    configure_logging("info", errors=errors)

    async def boom(request: Request) -> None:
        raise RuntimeError("a deliberate route failure")

    api.app.add_api_route("/boom", boom, methods=["POST"])
    await api.login()
    cookie = api.client.cookies["session"]

    with pytest.raises(RuntimeError):
        await api.client.post("/boom", json={"field": BODY})

    [event] = sentry_events.events
    [exception] = event["exception"]["values"]
    assert exception["value"] == "a deliberate route failure"
    assert exception["stacktrace"]["frames"][-1]["function"] == "boom"
    assert event["tags"]["service"] == "api"
    text = str(event)
    assert BODY not in text
    assert cookie not in text
    structlog.reset_defaults()
