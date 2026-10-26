"""The engine loop writes its heartbeat file, which Docker's health check watches, and
the same beat to Redis, which the dashboard shows (D45)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from redis.asyncio import Redis
from structlog.testing import capture_logs

from engine import main
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat

STARTED = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
SYMBOLS = ["BTC/USDT", "ETH/USDT"]


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)  # bytes, as the engine's own client
    await client.flushdb()
    yield client
    await client.aclose()


async def beat_once(heartbeat: Path, redis: Redis) -> None:
    """Run the heartbeat until it has touched its file, then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(
        main.heartbeat(
            stop, redis, mode="paper", exchange="mexc", symbols=SYMBOLS, started_at=STARTED
        )
    )
    # The first beat happens right away; the timeout only guards against a hang.
    async with asyncio.timeout(5):
        while not await asyncio.to_thread(heartbeat.exists):  # noqa: ASYNC110 (polling a file)
            await asyncio.sleep(0.01)
    stop.set()
    async with asyncio.timeout(5):
        await task  # returns promptly, without waiting out the interval


async def test_heartbeat_writes_its_file_and_redis_until_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redis: Redis
) -> None:
    heartbeat = tmp_path / "engine-heartbeat"
    monkeypatch.setattr(main, "HEARTBEAT_FILE", heartbeat)
    before = datetime.now(UTC)

    await beat_once(heartbeat, redis)

    raw = await redis.get(ENGINE_HEARTBEAT_KEY)
    assert raw is not None
    beat = EngineHeartbeat.model_validate_json(raw)
    assert (beat.beat, beat.mode, beat.started_at) == (1, "paper", STARTED)
    assert (beat.exchange, beat.symbols) == ("mexc", SYMBOLS)
    assert before <= beat.ts <= datetime.now(UTC)
    assert await redis.ttl(ENGINE_HEARTBEAT_KEY) == -1  # never expires: readers judge its age


async def test_without_redis_the_file_still_beats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "engine-heartbeat"
    monkeypatch.setattr(main, "HEARTBEAT_FILE", heartbeat)
    # Nothing listens on port 1: every Redis call fails.
    unreachable = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)

    with capture_logs() as logs:
        await beat_once(heartbeat, unreachable)
    await unreachable.aclose()

    assert any(log["event"] == "engine.heartbeat.redis_failed" for log in logs)
