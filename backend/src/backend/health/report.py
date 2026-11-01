"""Each service's state from what the API could read (pure, so every case is tested).

The engine, backtester and alerts service serve no HTTP: each beats into Redis every 10 s
(the engine since D45, the alerts service's status since D59, the backtester since D62),
and a beat older than its cutoff reads as `stale`. Ages are worked out against the API's
clock, as the Overview's are.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from backend.alerts.status import AlertsStatus
from shared.heartbeat import (
    BACKTESTER_STALE_SECONDS,
    ENGINE_STALE_SECONDS,
    EngineHeartbeat,
    ServiceHeartbeat,
)

# ok: alive. stale: its beat is too old (down or stuck). down: didn't answer.
# unknown: no beat to judge (never ran, or Redis is down). off: not set up.
State = Literal["ok", "stale", "down", "unknown", "off"]
ALERTS_STALE_SECONDS = 30  # its status is written every 10 s (backend/alerts/status.py)


class Probe(BaseModel):
    """One live check: did it answer, how fast, and if not, the error's type."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    ms: float | None = None
    error: str | None = None


class ServiceHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str  # api, db, redis, engine, backtester, alerts, sentry
    label: str  # as Settings → System shows it
    state: State
    last_seen: AwareDatetime | None = None  # its latest beat
    age_s: float | None = None  # how old that beat is, by the API's clock
    since: AwareDatetime | None = None  # when it started, if it says
    detail: str | None = None


class ServicesHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    checked_at: AwareDatetime
    services: list[ServiceHealth]


@dataclass(frozen=True)
class Readings:
    db: Probe
    redis: Probe
    engine: EngineHeartbeat | None
    backtester: ServiceHeartbeat | None
    alerts: AlertsStatus | None
    sentry: bool  # this API reports to Sentry (every service has the same SENTRY_DSN)


def _seconds(value: float) -> str:
    return f"{value:.0f} s" if value < 120 else f"{value / 60:.0f} min"


def _probe(name: str, label: str, probe: Probe) -> ServiceHealth:
    if probe.ok:
        return ServiceHealth(
            name=name, label=label, state="ok", detail=f"answered in {probe.ms:g} ms"
        )
    return ServiceHealth(name=name, label=label, state="down", detail=f"no answer ({probe.error})")


def _beat(
    name: str,
    label: str,
    now: datetime,
    ts: datetime | None,
    stale_after: float,
    *,
    redis_ok: bool,
    since: datetime | None = None,
    detail: str | None = None,
) -> ServiceHealth:
    if not redis_ok:
        return ServiceHealth(
            name=name, label=label, state="unknown", detail="Redis is down: its beat can't be read"
        )
    if ts is None:
        return ServiceHealth(
            name=name, label=label, state="unknown", detail="no heartbeat yet: has it run?"
        )
    age = round((now - ts).total_seconds(), 1)
    if age > stale_after:
        return ServiceHealth(
            name=name,
            label=label,
            state="stale",
            last_seen=ts,
            age_s=age,
            since=since,
            detail=f"no heartbeat for {_seconds(age)}: down or stuck",
        )
    return ServiceHealth(
        name=name, label=label, state="ok", last_seen=ts, age_s=age, since=since, detail=detail
    )


def _engine_detail(beat: EngineHeartbeat) -> str:
    bots = len(beat.bots)
    return (
        f"{beat.mode}, {bots} bot{'' if bots == 1 else 's'} running, "
        f"prices for {', '.join(beat.symbols) or 'no symbols'}"
    )


def _alerts_detail(status: AlertsStatus) -> str:
    if not status.configured:
        return "Telegram isn't set up: alerts are only logged"
    if status.last_error:
        return f"the last message didn't go: {status.last_error}"
    return "sending to Telegram"


def judge(now: datetime, readings: Readings) -> list[ServiceHealth]:
    """Every service's state, in the order Settings → System lists them."""
    engine, backtester, alerts = readings.engine, readings.backtester, readings.alerts
    redis_ok = readings.redis.ok
    return [
        ServiceHealth(name="api", label="API", state="ok", detail="answering"),
        _probe("db", "Database", readings.db),
        _probe("redis", "Redis", readings.redis),
        _beat(
            "engine",
            "Engine",
            now,
            None if engine is None else engine.ts,
            ENGINE_STALE_SECONDS,
            redis_ok=redis_ok,
            since=None if engine is None else engine.started_at,
            detail=None if engine is None else _engine_detail(engine),
        ),
        _beat(
            "backtester",
            "Backtester",
            now,
            None if backtester is None else backtester.ts,
            BACKTESTER_STALE_SECONDS,
            redis_ok=redis_ok,
            since=None if backtester is None else backtester.started_at,
        ),
        _beat(
            "alerts",
            "Alerts (Telegram)",
            now,
            None if alerts is None else alerts.ts,
            ALERTS_STALE_SECONDS,
            redis_ok=redis_ok,
            detail=None if alerts is None else _alerts_detail(alerts),
        ),
        ServiceHealth(
            name="sentry",
            label="Error reporting (Sentry)",
            state="ok" if readings.sentry else "off",
            detail="errors and crashes go to Sentry"
            if readings.sentry
            else "SENTRY_DSN isn't set: errors stay in the logs",
        ),
    ]
