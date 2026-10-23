"""Engine entry point. Run with `python -m engine`.

Runs a heartbeat loop, so Docker can tell the worker is alive, and one live price
stream per symbol (into Redis): PRICE_SYMBOLS plus the bots' symbols. Then it runs the
bots that are "running" in the database, on paper (D36), and feeds live prices to the
PaperBroker so resting orders fill. Bots are loaded once, at startup.
"""

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import TextIO

import structlog
from redis.asyncio import Redis

from engine.bots.runner import BotRunner
from engine.bots.store import load_running_bots
from engine.exchange.adapter import ExchangeAdapter
from engine.marketdata.prices import PriceStream, RedisPriceSink
from engine.marketdata.quotes import RedisQuotes
from engine.marketdata.store import CandleStore
from engine.risk.paper import PaperDesk
from shared.config import Settings, load_settings
from shared.db.session import make_engine

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
    db = make_engine(settings.database_url)
    try:
        # Public market data only: no API key, and trading stays off (D22).
        async with ExchangeAdapter() as exchange:
            bots = await load_running_bots(db, exchange.exchange_id)
            # dict.fromkeys drops repeats and keeps the order.
            bot_symbols = list(dict.fromkeys(bot.symbol for bot in bots))
            symbols = list(dict.fromkeys([*settings.price_symbols, *bot_symbols]))
            sink = RedisPriceSink(redis)
            quotes = RedisQuotes(redis)
            desk = PaperDesk(db, quotes, exchange)
            # A TaskGroup runs the tasks side by side and waits for all of them to end.
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(heartbeat(stop))
                workers = [
                    tasks.create_task(PriceStream(exchange, sink, symbol).run())
                    for symbol in symbols
                ]
                if bots:
                    runner = BotRunner(bots, desk.risk, exchange, quotes, CandleStore(db), db)
                    workers.append(tasks.create_task(runner.run()))
                    workers.append(
                        tasks.create_task(
                            desk.feed_prices(redis, exchange.exchange_id, bot_symbols)
                        )
                    )
                else:
                    log.info("bots.none", hint="python -m engine.bots.create adds one")
                await stop.wait()
                for worker in workers:
                    worker.cancel()
    finally:
        await redis.aclose()
        await db.dispose()
    log.info("engine.stopping")


def main() -> None:
    # First, so a bad config stops the engine before it does anything else.
    settings = load_settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))
