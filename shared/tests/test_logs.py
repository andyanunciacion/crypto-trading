"""The log mirror (shared/logs.py, D49): the engine's, backtester's and API's log lines
copied into a capped Redis stream for the dashboard's Logs page, without the trading
path ever waiting on Redis."""

import asyncio
import io
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
import structlog
from redis.asyncio import Redis

from shared.logs import LOGS_KEY, LogMirror, configure_logging, read_entry

# Nothing listens here, so every command fails at once, as with Redis down.
DOWN = "redis://127.0.0.1:1/0"


@pytest.fixture
def stdout() -> Iterator[io.StringIO]:
    """Where the logs print; structlog's global setup is put back afterwards."""
    stream = io.StringIO()
    yield stream
    structlog.reset_defaults()


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    await client.delete(LOGS_KEY)
    try:
        yield client
    finally:
        await client.aclose()


async def stream(redis: Redis) -> list[dict[str, object]]:
    entries = cast(list[tuple[bytes, dict[bytes, bytes]]], await redis.xrange(LOGS_KEY))
    return [json.loads(fields[b"json"]) for _, fields in entries]


async def test_info_and_above_reach_the_stream_with_their_service_and_fields(
    stdout: io.StringIO, redis: Redis
) -> None:
    mirror = LogMirror("engine")
    configure_logging("info", stdout, mirror=mirror)
    log = structlog.get_logger()

    log.info("bot.order", bot_id=3, side="buy", amount=Decimal("0.00000001"))
    log.warning("candles.live.late", symbol="BTC/USDT")
    log.error("bot.error", bot_id=3, ts=datetime(2026, 10, 28, 1, 2, 3, tzinfo=UTC))
    shipped = await mirror.flush(redis)

    lines = await stream(redis)
    assert shipped == 3
    assert [(line["event"], line["level"]) for line in lines] == [
        ("bot.order", "info"),
        ("candles.live.late", "warning"),
        ("bot.error", "error"),
    ]
    assert all(line["service"] == "engine" and "timestamp" in line for line in lines)
    # A Decimal or a datetime can't break the line; the Decimal stays exact, no exponent.
    assert lines[0]["amount"] == "0.00000001"
    assert lines[2]["ts"] == "2026-10-28T01:02:03+00:00"
    # stdout gets every line, as before.
    assert len(stdout.getvalue().splitlines()) == 3


async def test_debug_the_heartbeat_and_the_mirrors_own_lines_stay_out(
    stdout: io.StringIO, redis: Redis
) -> None:
    mirror = LogMirror("engine")
    configure_logging("debug", stdout, mirror=mirror)
    log = structlog.get_logger()

    log.debug("strategy.signal", action="hold")
    log.info("engine.heartbeat", beat=1)
    log.warning("logs.mirror.failed", error="ConnectionError")
    log.info("bots.loaded", count=1)
    await mirror.flush(redis)

    assert [line["event"] for line in await stream(redis)] == ["bots.loaded"]
    assert len(stdout.getvalue().splitlines()) == 4


async def test_a_full_buffer_drops_the_oldest_and_says_how_many(
    stdout: io.StringIO, redis: Redis
) -> None:
    mirror = LogMirror("engine", buffer=3)
    configure_logging("info", stdout, mirror=mirror)
    log = structlog.get_logger()

    for n in range(5):
        log.info("bot.order", n=n)
    await mirror.flush(redis)

    assert [line["n"] for line in await stream(redis)] == [2, 3, 4]
    printed = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["count"] for line in printed if line["event"] == "logs.mirror.dropped"] == [2]


async def test_with_redis_down_nothing_raises_and_the_lines_ship_once_it_is_back(
    stdout: io.StringIO, redis: Redis
) -> None:
    mirror = LogMirror("api")
    configure_logging("info", stdout, mirror=mirror)
    log = structlog.get_logger()
    down = Redis.from_url(DOWN, socket_connect_timeout=0.2)
    stop = asyncio.Event()

    log.info("api.kill.sent", id="k1")
    shipping = asyncio.create_task(mirror.ship(down, stop, every=0.05))
    await asyncio.sleep(0.3)
    log.info("api.kill.reply", id="k1")
    await asyncio.sleep(0.3)
    stop.set()
    await shipping  # returned, didn't raise
    await down.aclose()

    printed = [json.loads(line)["event"] for line in stdout.getvalue().splitlines()]
    assert printed.count("logs.mirror.failed") == 1  # once per outage, not every try
    await mirror.flush(redis)
    assert [line["event"] for line in await stream(redis)] == ["api.kill.sent", "api.kill.reply"]


async def test_ship_sends_on_its_own_and_flushes_when_stopped(
    stdout: io.StringIO, redis: Redis
) -> None:
    mirror = LogMirror("backtester")
    configure_logging("info", stdout, mirror=mirror)
    log = structlog.get_logger()
    stop = asyncio.Event()

    shipping = asyncio.create_task(mirror.ship(redis, stop, every=0.05))
    log.info("backtest.job.received", job="j1")
    await asyncio.sleep(0.3)
    assert [line["event"] for line in await stream(redis)] == ["backtest.job.received"]

    log.info("backtest.job.done", job="j1")
    stop.set()
    await shipping

    assert [line["event"] for line in await stream(redis)][-1] == "backtest.job.done"


async def test_the_stream_is_capped(stdout: io.StringIO, redis: Redis) -> None:
    mirror = LogMirror("engine", maxlen=10)
    configure_logging("info", stdout, mirror=mirror)
    log = structlog.get_logger()

    for n in range(1000):
        log.info("bot.order", n=n)
    await mirror.flush(redis)

    # Trimmed approximately (whole nodes of 100 entries), which is cheap for Redis.
    assert 10 <= await redis.xlen(LOGS_KEY) < 1000


def test_an_entry_is_read_back_with_its_parts_and_the_rest_as_fields() -> None:
    line = json.dumps(
        {
            "event": "risk.decision",
            "level": "info",
            "timestamp": "2026-10-28T01:02:03.000004Z",
            "service": "engine",
            "bot_id": 7,
            "decision": "blocked",
        }
    )

    entry = read_entry("1761613323000-0", line.encode())

    assert entry is not None
    assert (entry.id, entry.event, entry.level, entry.service, entry.bot_id) == (
        "1761613323000-0",
        "risk.decision",
        "info",
        "engine",
        7,
    )
    assert entry.ts == datetime(2026, 10, 28, 1, 2, 3, 4, tzinfo=UTC)
    assert entry.fields == {"decision": "blocked"}


@pytest.mark.parametrize(
    "line",
    [b"not json", b"[1, 2]", b'{"level": "info"}', b'{"event": "x", "bot_id": "seven"}'],
)
def test_a_line_that_does_not_read_is_skipped(line: bytes) -> None:
    assert read_entry("1-0", line) is None
