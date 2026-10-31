"""The Backtests page (D48): run a backtest, follow it, and show the stored runs.

All of it is on the `protected` router (app.py): a session is checked before anything
here runs.

- `POST /backtests`: queues a `RunBacktestCommand` for the backtester service
  (shared/backtest_jobs.py) and answers 202 with the job at once. A run can take tens
  of seconds, longer than a request should wait. The API never runs one: the backtester
  checks everything, as `engine.backtest.run` does, and fails the job with the reason.
- `GET /backtests/jobs/{job_id}`: how the job is going (`queued`, `running`, `done` with
  the run's id, `failed` with why). 404 once its record has expired (an hour).
- `GET /backtests?limit=&before=`: the stored runs, newest first, with their metrics.
- `GET /backtests/{run_id}`: one run: its inputs, metrics, trades, equity curve and
  skipped orders (store.py).
- `GET /candles/range?symbol=&timeframe=`: what's stored of a series, for the run form.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, cast
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from redis.asyncio import Redis
from starlette.datastructures import State

from backend.auth.deps import CurrentUser
from backend.backtests.store import (
    CandleRange,
    RunDetail,
    RunSummary,
    read_candle_range,
    read_run,
    read_runs,
)
from shared.backtest_jobs import BacktestJob, RunBacktestCommand, queue, read_job
from shared.regime import AutoSwitchSettings

log = structlog.get_logger()

router = APIRouter(tags=["backtests"])

SOURCE = "dashboard"
DEFAULT_RUNS = 20
MAX_RUNS = 100


def _all_in() -> dict[str, JsonValue]:
    return {"mode": "all_in"}


class BacktestRequest(BaseModel):
    """`engine.backtest.run`'s arguments. The page fills in the CLI's defaults."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=1, max_length=64)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    symbol: str = Field(min_length=3, max_length=32)
    timeframe: str = Field(min_length=1, max_length=8)
    days: int = Field(ge=1, le=3650)
    cash: Decimal = Field(gt=0)
    fee_rate: Decimal = Field(ge=0, lt=1)
    slippage: Decimal | None = Field(default=None, ge=0, lt=1)  # None: the pair's default
    sizing: dict[str, JsonValue] = Field(default_factory=_all_in)
    stops: dict[str, JsonValue] = Field(default_factory=dict)
    # `--auto-switch` (P6-5c): every regime mapped is checked here, each config's params
    # by the backtester.
    auto_switch: AutoSwitchSettings | None = None


def _now(state: State) -> datetime:
    return cast(Callable[[], datetime], state.clock)()


@router.post("/backtests", status_code=status.HTTP_202_ACCEPTED)
async def run(body: BacktestRequest, request: Request, user: CurrentUser) -> BacktestJob:
    state = request.app.state
    command = RunBacktestCommand(**body.model_dump(), source=SOURCE, sent_at=_now(state))
    job = await queue(cast(Redis, state.redis), command)
    log.info("api.backtest.queued", user=user.username, **command.model_dump(mode="json"))
    return job


@router.get("/backtests/jobs/{job_id}")
async def job(job_id: UUID, request: Request) -> BacktestJob:
    found = await read_job(cast(Redis, request.app.state.redis), job_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no backtest job {job_id}")
    return found


@router.get("/backtests")
async def runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = DEFAULT_RUNS,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> list[RunSummary]:
    async with request.app.state.db.connect() as conn:
        return await read_runs(conn, limit=limit, before=before)


@router.get("/backtests/{run_id}")
async def run_detail(run_id: int, request: Request) -> RunDetail:
    async with request.app.state.db.connect() as conn:
        found = await read_run(conn, run_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no backtest run {run_id}")
    return found


@router.get("/candles/range")
async def candle_range(
    request: Request,
    symbol: Annotated[str, Query(min_length=3, max_length=32)],
    timeframe: Annotated[str, Query(min_length=1, max_length=8)],
    exchange: Annotated[str, Query(min_length=1, max_length=32)] = "mexc",
) -> CandleRange:
    async with request.app.state.db.connect() as conn:
        return await read_candle_range(conn, exchange, symbol, timeframe)
