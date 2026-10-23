"""Engine entry point. Run with `python -m engine`.

Runs a heartbeat loop, so Docker can tell the worker is alive, and one live price
stream per symbol in PRICE_SYMBOLS (into Redis). Strategies, the RiskManager and
brokers plug into it in later phases.
"""

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import TextIO

import structlog
from redis.asyncio import Redis

from engine.exchange.adapter import ExchangeAdapter
from engine.marketdata.prices import PriceStream, RedisPriceSink
from shared.config import Settings, load_settings

HEARTBEAT_INTERVAL_SECONDS = 10
# The Docker health check reads this file's modification time (see compose.yaml).
HEARTBEAT_FILE = Path("/tmp/engine-heartbeat")

log = structlog.get_logger()


def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """JSON logs to `stream` (default stdout). CLIs whose stdout is data log to stderr."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        # Drops calls below LOG_LEVEL before they do any work.
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(stream),
    )


async def heartbeat(stop: asyncio.Event) -> None:
    beat = 0
    while not stop.is_set():
        beat += 1
        # File I/O blocks; in a thread it can't stall the event loop.
        await asyncio.to_thread(HEARTBEAT_FILE.touch)
        log.info("engine.heartbeat", beat=beat)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)


async def run(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Python running as PID 1 in a container ignores SIGTERM unless it has a handler,
    # so without this `docker compose stop` would wait 10 s and then kill the engine.
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows event loops don't support signal handlers; Ctrl+C still works.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info(
        "engine.starting",
        mode=settings.mode,
        heartbeat_interval_s=HEARTBEAT_INTERVAL_SECONDS,
        price_symbols=settings.price_symbols,
    )
    redis = Redis.from_url(settings.redis_url)
    try:
        # Public market data only: no API key, and trading stays off (D22).
        async with ExchangeAdapter() as exchange:
            sink = RedisPriceSink(redis)
            # A TaskGroup runs the tasks side by side and waits for all of them to end.
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(heartbeat(stop))
                streams = [
                    tasks.create_task(PriceStream(exchange, sink, symbol).run())
                    for symbol in settings.price_symbols
                ]
                await stop.wait()
                for stream in streams:
                    stream.cancel()
    finally:
        await redis.aclose()
    log.info("engine.stopping")


def main() -> None:
    # First, so a bad config stops the engine before it does anything else.
    settings = load_settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))
