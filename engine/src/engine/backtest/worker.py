"""The backtester service: runs the dashboard's backtests (D48). `python -m engine.backtest.worker`.

It runs apart from the trading engine, in its own container, because a backtest is
CPU-bound and can take a while: in the engine it would hold up prices, stops and the
kill switch. Here a long run only holds up the next backtest.

It pops `RunBacktestCommand`s from `backtest:commands` (shared/backtest_jobs.py), one at
a time, oldest first, and runs each exactly as `python -m engine.backtest.run` would with
the same arguments: the same config (`cli.build_config`, with the exchange's market
rules fetched fresh), the same run and store (`run.backtest`). So the dashboard shows the
same numbers as the CLI. The job's record says how it's going: `running`, then `done`
with the run's id, or `failed` with the reason.

At startup it also writes the catalog the run form is drawn from (control/catalog.py),
so the Backtests page works while the trading engine is down. A heartbeat file tells
Docker it's alive (see compose.yaml).
"""

import asyncio
import contextlib
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backoff import Backoff
from engine.backtest.cli import (
    MarketRulesSource,
    NotEnoughHistory,
    PublicRules,
    RunSettings,
    build_config,
)
from engine.backtest.run import backtest
from engine.backtest.simulator import BacktestError
from engine.backtest.store import BacktestRunStore
from engine.control.bots import describe
from engine.control.catalog import publish as publish_catalog
from engine.exchange.errors import ExchangeError
from engine.marketdata.store import CandleStore
from engine.strategies.registry import build, strategy_class
from shared.backtest_jobs import (
    BACKTEST_COMMANDS_KEY,
    BacktestJob,
    RunBacktestCommand,
    read_job,
    write_job,
)
from shared.commands import BLOCK_SECONDS
from shared.config import Settings, load_settings
from shared.db.session import make_engine
from shared.logs import configure_logging

HEARTBEAT_INTERVAL_SECONDS = 10
# The Docker health check reads this file's modification time (see compose.yaml).
HEARTBEAT_FILE = Path("/tmp/backtester-heartbeat")

log = structlog.get_logger()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Refused(Exception):
    """A backtest that can't run as asked, and why (shown on the Backtests page); with
    too little history, the most days that would fit."""

    def __init__(self, message: str, max_days: int | None = None) -> None:
        super().__init__(message)
        self.max_days = max_days


class BacktestWorker:
    def __init__(
        self,
        redis: Redis,
        db: AsyncEngine,
        *,
        exchange_id: str = "mexc",
        rules: MarketRulesSource | None = None,
        backoff: Backoff | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._redis = redis
        self._candles = CandleStore(db)
        self._runs = BacktestRunStore(db)
        self._exchange_id = exchange_id
        self._rules = rules or PublicRules(exchange_id)
        self._backoff = backoff or Backoff()
        self._clock = clock

    async def run(self) -> None:
        """Run backtests as they come, until cancelled."""
        log.info("backtest.worker.listening", key=BACKTEST_COMMANDS_KEY)
        while True:
            try:
                # Returns at once when a command comes; else asks again (BLOCK_SECONDS).
                popped = await self._redis.brpop([BACKTEST_COMMANDS_KEY], timeout=BLOCK_SECONDS)
                self._backoff.reset()
                if popped is not None:
                    await self.handle(popped[1])
            except Exception as exc:  # cancelling raises CancelledError, which isn't caught
                delay = self._backoff.next_delay()
                log.warning(
                    "backtest.worker.reconnecting",
                    attempt=self._backoff.attempts,
                    delay_s=round(delay, 2),
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                await asyncio.sleep(delay)

    async def handle(self, raw: bytes | str) -> BacktestJob | None:
        """Run one command's backtest; returns its job as it ended (None if it didn't parse)."""
        started = time.monotonic()
        try:
            command = RunBacktestCommand.model_validate_json(raw)
        except ValidationError as exc:
            # With no id, there's no job to fail.
            log.error("backtest.job.invalid", error="ValidationError", detail=str(exc))
            return None
        log.info("backtest.job.received", **command.model_dump(mode="json"))
        # The record expires an hour after it was queued; a command that waited longer
        # (the backtester was down) gets a new one.
        queued = await read_job(self._redis, command.id) or BacktestJob(
            id=command.id, status="queued", queued_at=command.sent_at
        )
        job = queued.model_copy(update={"status": "running", "started_at": self._clock()})
        await write_job(self._redis, job)
        try:
            run_id = await self._run(command)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, Refused) else f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, Refused):
                log.error("backtest.job.error", id=str(command.id), error=type(exc).__name__)
            job = job.model_copy(
                update={
                    "status": "failed",
                    "error": reason,
                    "max_days": exc.max_days if isinstance(exc, Refused) else None,
                    "finished_at": self._clock(),
                }
            )
            log.warning("backtest.job.failed", id=str(command.id), reason=reason)
        else:
            job = job.model_copy(
                update={"status": "done", "run_id": run_id, "finished_at": self._clock()}
            )
            log.info(
                "backtest.job.done",
                id=str(command.id),
                run_id=run_id,
                took_ms=round((time.monotonic() - started) * 1000),
            )
        await write_job(self._redis, job)
        return job

    async def _run(self, command: RunBacktestCommand) -> int:
        """Check the command as the CLI's arguments are checked, then run and store it."""
        try:
            strategy = build(
                command.strategy, strategy_class(command.strategy).version, command.params
            )
            # The timeframe, sizing and stops arrive as JSON; RunSettings reads them.
            fields = {
                "symbol",
                "timeframe",
                "days",
                "cash",
                "fee_rate",
                "slippage",
                "sizing",
                "stops",
            }
            settings = RunSettings.model_validate(
                command.model_dump(include=fields) | {"exchange": self._exchange_id}
            )
        except ValidationError as exc:
            raise Refused(describe(exc)) from None
        except ValueError as exc:  # an unknown strategy
            raise Refused(str(exc)) from None
        if "/" not in command.symbol:
            raise Refused(f"{command.symbol!r} isn't a symbol like BTC/USDT")
        try:
            config = await build_config(settings, self._candles, strategy, self._rules)
            run_id, _ = await backtest(self._candles, self._runs, strategy, config)
        except NotEnoughHistory as exc:
            raise Refused(str(exc), exc.max_days) from None
        except BacktestError as exc:  # no candles, a gap
            raise Refused(str(exc)) from None
        except ValidationError as exc:  # the config: a range off the candle boundaries
            raise Refused(describe(exc)) from None
        except ExchangeError as exc:  # e.g. a symbol the exchange doesn't list
            raise Refused(f"{command.symbol}: {exc}") from None
        return run_id


async def beat(stop: asyncio.Event) -> None:
    """Touch the file Docker's health check watches."""
    while not stop.is_set():
        # File I/O blocks; in a thread it can't stall the event loop.
        await asyncio.to_thread(HEARTBEAT_FILE.touch)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)


async def run(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # As in engine/main.py: PID 1 in a container ignores SIGTERM without a handler.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    log.info("backtest.worker.starting")
    redis = Redis.from_url(settings.redis_url)
    db = make_engine(settings.database_url)
    try:
        await publish_catalog(redis, datetime.now(UTC))
        worker = BacktestWorker(redis, db)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(beat(stop))
            working = tasks.create_task(worker.run())
            await stop.wait()
            working.cancel()
    finally:
        await redis.aclose()
        await db.dispose()
    log.info("backtest.worker.stopping")


def main() -> None:
    # First, so a bad config stops the worker before it does anything else.
    settings = load_settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
