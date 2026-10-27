"""Backtests run from the dashboard, as jobs for the backtester service (D48).

A backtest is CPU-bound and can take a while (a month of 1m candles: tens of seconds),
so it never runs in the trading engine, whose prices, stops and kill switch can't wait
for it. The backtester service (engine/backtest/worker.py) runs them instead, one at a
time.

- The API `queue()`s a `RunBacktestCommand`: it writes the job as `queued` at
  `job_key(id)`, then `LPUSH`es the command onto `BACKTEST_COMMANDS_KEY`.
- The backtester `BRPOP`s it (oldest first) and writes the job as it goes: `running`,
  then `done` with the stored run's id, or `failed` with the reason.
- The browser polls the job through the API (`read_job()`).

Unlike the bot commands, these never expire: a backtest only reads candles and writes
`backtest_runs`, so one queued while the backtester was down can safely run later. A
job's record expires `JOB_TTL_SECONDS` after its last change; the run itself is kept.
"""

from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue
from redis.asyncio import Redis

BACKTEST_COMMANDS_KEY = "backtest:commands"
JOB_TTL_SECONDS = 3600


def job_key(job_id: UUID) -> str:
    """The Redis key holding a job's `BacktestJob`, e.g. `backtest:job:<uuid>`."""
    return f"backtest:job:{job_id}"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


_ALL_IN: dict[str, JsonValue] = {"mode": "all_in"}


class RunBacktestCommand(_Frozen):
    """Backtest `strategy` on the stored candles of the last `days`, as
    `python -m engine.backtest.run` would with the same arguments. The backtester checks
    everything (the strategy's params, sizing, stops, that the candles are there) and
    fails the job with the reason."""

    type: Literal["run_backtest"] = "run_backtest"
    id: UUID = Field(default_factory=uuid4)
    source: str = Field(min_length=1, max_length=64)
    sent_at: AwareDatetime
    strategy: str = Field(min_length=1, max_length=64)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    symbol: str = Field(min_length=3, max_length=32)  # CCXT format, e.g. BTC/USDT
    timeframe: str = Field(min_length=1, max_length=8)
    days: int = Field(ge=1)
    cash: Decimal = Field(gt=0)
    fee_rate: Decimal = Field(ge=0, lt=1)  # of each fill's value
    slippage: Decimal | None = Field(default=None, ge=0, lt=1)  # None: the pair's default
    # As backtest_runs.sizing and .stops store them; the backtester reads them.
    sizing: dict[str, JsonValue] = Field(default_factory=lambda: _ALL_IN.copy())
    stops: dict[str, JsonValue] = Field(default_factory=dict)


JobStatus = Literal["queued", "running", "done", "failed"]


class BacktestJob(_Frozen):
    id: UUID  # the command's
    status: JobStatus
    queued_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    run_id: int | None = None  # when done: the stored run
    error: str | None = None  # when failed: why
    # When it failed for too little stored history: the most days that would fit.
    max_days: int | None = None


async def write_job(redis: Redis, job: BacktestJob) -> None:
    await redis.set(job_key(job.id), job.model_dump_json(), ex=JOB_TTL_SECONDS)


async def read_job(redis: Redis, job_id: UUID) -> BacktestJob | None:
    raw = await redis.get(job_key(job_id))
    return None if raw is None else BacktestJob.model_validate_json(raw)


async def queue(redis: Redis, command: RunBacktestCommand) -> BacktestJob:
    """Queue `command` for the backtester; returns the job, `queued`."""
    job = BacktestJob(id=command.id, status="queued", queued_at=command.sent_at)
    # The job first: the backtester may pop the command at once and move it on.
    await write_job(redis, job)
    await redis.lpush(BACKTEST_COMMANDS_KEY, command.model_dump_json())
    return job
