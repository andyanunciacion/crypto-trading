"""The Logs page (P5-6, D49): what the services logged, and what the risk layer blocked.

All of it is on the `protected` router (app.py): a session is checked before anything
here runs.

- `GET /logs?level=&event=&bot_id=&service=&before=&limit=`: the mirrored log lines,
  newest first (store.py). `level` is that level and above; `event` is a prefix
  (`risk.`, `kill_switch.`), repeatable for any of several; `before` is the `next` of
  the page before.
- `GET /risk-events?bot_id=&rule=&decision=&start=&end=&before=&limit=`: `risk_events`
  rows, newest first, with the inputs each was decided on.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Query, Request
from redis.asyncio import Redis

from backend.logs.store import (
    LogPage,
    LogQuery,
    RiskEventQuery,
    RiskEventView,
    read_logs,
    read_risk_events,
)

router = APIRouter(tags=["logs"])


@router.get("/logs")
async def logs(request: Request, query: Annotated[LogQuery, Query()]) -> LogPage:
    return await read_logs(cast(Redis, request.app.state.redis), query)


@router.get("/risk-events")
async def risk_events(
    request: Request, query: Annotated[RiskEventQuery, Query()]
) -> list[RiskEventView]:
    async with request.app.state.db.connect() as conn:
        return await read_risk_events(conn, query)
