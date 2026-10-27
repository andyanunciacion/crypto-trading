"""The backtester service (backtest/worker.py, D48), against a real TimescaleDB and Redis
(see shared.testing). The market rules are a fake; the candles are test_backtest_store's
wave: 300 hours of BTC/USDT 1h, which EMA crossover 3/8 trades both ways.
"""

import argparse
import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backtest.cli import add_run_arguments, json_object_arg, run_settings
from engine.backtest.run import backtest
from engine.backtest.store import BacktestRunStore
from engine.backtest.worker import BacktestWorker
from engine.exchange.errors import ExchangeError
from engine.exchange.models import Candle, MarketRules
from engine.marketdata.store import CandleStore, Series
from engine.strategies.registry import build, strategy_class
from shared.backtest_jobs import (
    BACKTEST_COMMANDS_KEY,
    RunBacktestCommand,
    job_key,
    queue,
    read_job,
)
from shared.db.session import make_engine

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
HOURS = 300
BTC = Series("mexc", "BTC/USDT", "1h")
NOW = T0 + HOURS * HOUR + timedelta(minutes=5)
RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=Decimal("0.000001"),
    price_step=Decimal("0.01"),
    min_amount=Decimal("0.000001"),
    min_cost=Decimal("1"),
)


def wave(hour: int) -> Candle:
    # A slow sine wave with a fast wobble on top, so the EMAs cross several times.
    close = round(30000 + 2000 * math.sin(hour / 20) + 150 * math.sin(hour), 2)
    open_ = round(close * (1 + 0.001 * math.cos(hour)), 2)
    o, c = Decimal(str(open_)), Decimal(str(close))
    return Candle(
        ts=T0 + hour * HOUR,
        open=o,
        high=max(o, c) + 10,
        low=min(o, c) - 10,
        close=c,
        volume=Decimal(1),
    )


class FakeRules:
    async def market_rules(self, symbol: str) -> MarketRules:
        if symbol != "BTC/USDT":
            raise ExchangeError(f"mexc does not have market symbol {symbol}")
        return RULES


@dataclass(frozen=True)
class Setup:
    db: AsyncEngine
    redis: Redis
    worker: BacktestWorker


@pytest.fixture
async def setup(db_url: URL, redis_url: str) -> AsyncIterator[Setup]:
    db = make_engine(db_url)
    async with db.begin() as conn:
        await conn.execute(text("TRUNCATE candles, backtest_runs"))
    await CandleStore(db).upsert(BTC, [wave(h) for h in range(HOURS)])
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    yield Setup(db, redis, BacktestWorker(redis, db, rules=FakeRules(), clock=lambda: NOW))
    await redis.aclose()
    await db.dispose()


def command(**overrides: Any) -> RunBacktestCommand:
    values: dict[str, Any] = {
        "source": "dashboard",
        "sent_at": NOW,
        "strategy": "ema_crossover",
        "params": {"fast": 3, "slow": 8},
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "days": 10,
        "cash": Decimal(1000),
        "fee_rate": Decimal("0.001"),
    }
    return RunBacktestCommand(**(values | overrides))


async def test_a_job_runs_and_stores_exactly_what_the_cli_would(setup: Setup) -> None:
    job = await setup.worker.handle(command().model_dump_json())

    assert job is not None and job.status == "done" and job.run_id is not None
    # The CLI's path with the same arguments: python -m engine.backtest.run ema_crossover
    # BTC/USDT 1h --days 10 --params '{"fast": 3, "slow": 8}' (cash and fee its defaults).
    parser = argparse.ArgumentParser()
    add_run_arguments(parser)
    parser.add_argument("--params", type=json_object_arg, default={})
    args = parser.parse_args(["BTC/USDT", "1h", "--days", "10", "--params", '{"fast":3,"slow":8}'])
    strategy = build("ema_crossover", strategy_class("ema_crossover").version, args.params)
    candles, runs = CandleStore(setup.db), BacktestRunStore(setup.db)
    config = await run_settings(args, candles, strategy, FakeRules())
    cli_id, _ = await backtest(candles, runs, strategy, config)

    ours, theirs = await runs.load(job.run_id), await runs.load(cli_id)
    assert ours.config == theirs.config
    assert ours.candles_sha256 == theirs.candles_sha256
    assert ours.result == theirs.result
    assert len(ours.result.fills) >= 4  # the wave really traded, both ways


async def test_the_job_goes_from_queued_to_running_to_done(setup: Setup) -> None:
    sent = command(sizing={"mode": "percent_of_equity", "percent": 50})
    queued = await queue(setup.redis, sent)
    assert queued.status == "queued"
    assert await setup.redis.llen(BACKTEST_COMMANDS_KEY) == 1

    running = asyncio.create_task(setup.worker.run())
    try:
        async with asyncio.timeout(10):
            unfinished = ("queued", "running")
            while (job := await read_job(setup.redis, sent.id)) is None or job.status in unfinished:  # noqa: ASYNC110 (polling Redis)
                await asyncio.sleep(0.05)
    finally:
        running.cancel()

    assert job.status == "done"
    assert job.queued_at == NOW and job.started_at == NOW and job.finished_at == NOW
    stored = await BacktestRunStore(setup.db).load(job.run_id or 0)
    assert stored.config.sizing.mode == "percent_of_equity"
    assert 0 < await setup.redis.ttl(job_key(sent.id)) <= 3600  # the record expires


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"params": {"fast": 30}}, "fast (30) must be shorter than slow (21)"),
        ({"strategy": "moon"}, "moon"),
        ({"timeframe": "2h"}, "timeframe"),
        ({"sizing": {"mode": "fixed"}}, "amount"),
        ({"stops": {"take_profit": {"kind": "r", "multiple": 2}}}, "needs a stop_loss"),
        ({"symbol": "BTCUSDT"}, "isn't a symbol like BTC/USDT"),
        ({"symbol": "ETH/USDT"}, "no candles stored"),
        ({"days": 12}, "warm-up"),  # 288 of the 300 candles leave too few before them
    ],
    ids=[
        "params",
        "strategy",
        "timeframe",
        "sizing",
        "stops",
        "symbol",
        "no-candles",
        "warm-up",
    ],
)
async def test_a_backtest_that_cant_run_fails_with_the_reason(
    setup: Setup, overrides: dict[str, Any], reason: str
) -> None:
    job = await setup.worker.handle(command(**overrides).model_dump_json())

    assert job is not None and job.status == "failed"
    assert job.error is not None and reason in job.error
    assert job.run_id is None
    async with setup.db.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM backtest_runs"))).scalar() == 0


async def test_too_little_history_says_how_many_days_fit_and_they_do(setup: Setup) -> None:
    # 300 hours stored. EMA 3/8 warms up on 25 candles, so the range can start 25 hours
    # in at the earliest: 275 hours before the end, 11 whole days.
    warmup = build("ema_crossover", 1, {"fast": 3, "slow": 8}).warmup_candles
    assert warmup == 25

    refused = await setup.worker.handle(command(days=12).model_dump_json())

    assert refused is not None and refused.status == "failed" and refused.max_days == 11
    assert refused.error == (
        "needs 25 warm-up candles before the start (2025-01-01 12:00 UTC), but the stored"
        " mexc BTC/USDT 1h candles start at 2025-01-01 00:00 UTC: use at most 11 days, or"
        " backfill more (--days 14)"
    )
    done = await setup.worker.handle(command(days=refused.max_days).model_dump_json())
    assert done is not None and done.status == "done"


async def test_with_no_room_at_all_it_only_says_to_backfill(setup: Setup) -> None:
    job = await setup.worker.handle(command(params={"fast": 9, "slow": 200}).model_dump_json())

    assert job is not None and job.status == "failed" and job.max_days is None
    # 601 warm-up candles plus the 240-hour range: 841 hours, so 36 days back.
    assert job.error is not None and job.error.endswith(": backfill more (--days 36)")


async def test_a_symbol_the_exchange_doesnt_list_fails(setup: Setup) -> None:
    await CandleStore(setup.db).upsert(Series("mexc", "NOPE/USDT", "1h"), [wave(0)])

    job = await setup.worker.handle(command(symbol="NOPE/USDT", days=1).model_dump_json())

    assert job is not None and job.status == "failed"
    assert job.error == "NOPE/USDT: mexc does not have market symbol NOPE/USDT"


async def test_a_command_that_doesnt_parse_is_dropped(setup: Setup) -> None:
    assert await setup.worker.handle(b'{"type": "run_backtest"}') is None
    assert await setup.redis.keys("backtest:job:*") == []
