"""The API's health routes (P7-4, D62).

- `GET /health` (public, `public`): Docker's health check. Checks the database and Redis,
  2 s each, side by side: 200 `{"status": "ok", ...}`, or 503 saying which is down.
  Anyone can call it, so it says only ok or down, never why. A change of state is logged
  once (`api.health.failed` / `.recovered`), not on every check.
- `GET /health/services` (signed in, `router`): every service's state for Settings →
  System (report.py), with why.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

import sentry_sdk
import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.alerts.status import ALERTS_STATUS_KEY, AlertsStatus
from backend.health.report import Probe, Readings, ServicesHealth, judge
from shared.heartbeat import (
    BACKTESTER_HEARTBEAT_KEY,
    ENGINE_HEARTBEAT_KEY,
    EngineHeartbeat,
    ServiceHeartbeat,
)

PROBE_SECONDS = 2.0  # Docker's check gives /health 5 s

log = structlog.get_logger()

public = APIRouter(tags=["health"])
router = APIRouter(tags=["health"])


async def _timed(check: Callable[[], Awaitable[Any]]) -> Probe:
    """Whether `check` finished within PROBE_SECONDS, and how fast."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            await check()
    except Exception as exc:  # down, refused, timed out: all mean it didn't answer
        return Probe(ok=False, error=type(exc).__name__)
    return Probe(ok=True, ms=round((time.perf_counter() - started) * 1000, 1))


async def probe_db(db: AsyncEngine) -> Probe:
    async def select_one() -> None:
        async with db.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return await _timed(select_one)


async def probe_redis(redis: Redis) -> Probe:
    async def ping() -> None:
        await redis.ping()

    return await _timed(ping)


def _probes(request: Request) -> Awaitable[tuple[Probe, Probe]]:
    state = request.app.state
    return asyncio.gather(probe_db(state.db), probe_redis(cast(Redis, state.redis)))


@public.get(
    "/health",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "The database or Redis is down"}
    },
)
async def health(request: Request) -> JSONResponse:
    """Docker's health check: is the API up, with its database and Redis?"""
    db, redis = await _probes(request)
    parts = {"db": db, "redis": redis}
    down = frozenset(name for name, probe in parts.items() if not probe.ok)
    was_down: frozenset[str] = getattr(request.app.state, "health_down", frozenset())
    if down != was_down:
        request.app.state.health_down = down
        if down:
            log.warning(
                "api.health.failed",
                down=sorted(down),
                errors={name: parts[name].error for name in sorted(down)},
            )
        else:
            log.info("api.health.recovered", was_down=sorted(was_down))
    return JSONResponse(
        {
            "status": "down" if down else "ok",
            **{name: "down" if name in down else "ok" for name in parts},
        },
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE if down else status.HTTP_200_OK,
    )


async def _read[M: BaseModel](redis: Redis, key: str, model: type[M]) -> M | None:
    """The model stored at `key`, or None if it's missing, unreadable or Redis fails."""
    try:
        raw = await asyncio.wait_for(redis.get(key), timeout=PROBE_SECONDS)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        return None


@router.get("/health/services")
async def services(request: Request) -> ServicesHealth:
    """Every service's state, for Settings → System."""
    now = cast(Callable[[], datetime], request.app.state.clock)()
    redis = cast(Redis, request.app.state.redis)
    (db, redis_probe), engine, backtester, alerts = await asyncio.gather(
        _probes(request),
        _read(redis, ENGINE_HEARTBEAT_KEY, EngineHeartbeat),
        _read(redis, BACKTESTER_HEARTBEAT_KEY, ServiceHeartbeat),
        _read(redis, ALERTS_STATUS_KEY, AlertsStatus),
    )
    readings = Readings(
        db=db,
        redis=redis_probe,
        engine=engine,
        backtester=backtester,
        alerts=alerts,
        sentry=sentry_sdk.get_client().is_active(),
    )
    return ServicesHealth(checked_at=now, services=judge(now, readings))
