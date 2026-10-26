"""Engine entry point. Run with `python -m engine`.

Runs a heartbeat loop, so Docker and the dashboard (D45) can tell the worker is alive,
and one live price stream per symbol (into Redis): PRICE_SYMBOLS, the bots' symbols,
and every coin a paper position holds. Then it runs the bots that are "running" or "paused" in the
database, on paper (D36, D42), and feeds live prices to the PaperBroker so resting
orders fill. Bots are loaded once, at startup, and orders left
open from before the engine started are cancelled first (D38).

It also listens for commands over Redis (control/listener.py, D43): the kill switch.
Commands sent while it was down are carried out first, before any bot is loaded.
"""

import asyncio
import contextlib
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog
from redis.asyncio import Redis

from engine.bots.runner import BotRunner
from engine.bots.store import held_symbols, load_bots
from engine.control.listener import CommandListener
from engine.exchange.adapter import ExchangeAdapter
from engine.marketdata.prices import PriceStream, RedisPriceSink
from engine.marketdata.quotes import RedisQuotes
from engine.marketdata.store import CandleStore
from engine.risk.kill import KillSwitch
from engine.risk.paper import PaperDesk
from shared.config import Settings, load_settings
from shared.db.session import make_engine
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat
from shared.logs import configure_logging
from shared.prices import PriceUpdate

HEARTBEAT_INTERVAL_SECONDS = 10
# The Docker health check reads this file's modification time (see compose.yaml).
HEARTBEAT_FILE = Path("/tmp/engine-heartbeat")

log = structlog.get_logger()


async def heartbeat(
    stop: asyncio.Event,
    redis: Redis,
    *,
    mode: Literal["paper", "live"],
    exchange: str,
    symbols: list[str],
    started_at: datetime,
) -> None:
    """Touch the file Docker's health check watches, and tell the dashboard (D45), every
    beat: with the symbols whose prices it streams, so the dashboard knows which to
    watch. Without Redis the file still beats: the engine itself is alive."""
    beat = 0
    while not stop.is_set():
        beat += 1
        # File I/O blocks; in a thread it can't stall the event loop.
        await asyncio.to_thread(HEARTBEAT_FILE.touch)
        payload = EngineHeartbeat(
            ts=datetime.now(UTC),
            started_at=started_at,
            mode=mode,
            beat=beat,
            exchange=exchange,
            symbols=symbols,
        )
        try:
            await redis.set(ENGINE_HEARTBEAT_KEY, payload.model_dump_json())
        except Exception as exc:
            log.warning(
                "engine.heartbeat.redis_failed",
                beat=beat,
                error=type(exc).__name__,
                detail=str(exc),
            )
        log.info("engine.heartbeat", beat=beat)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)


async def run(settings: Settings) -> None:
    started_at = datetime.now(UTC)
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
            sink = RedisPriceSink(redis)
            quotes = RedisQuotes(redis)
            desk = PaperDesk(db, quotes, exchange)
            kill_switch = KillSwitch(desk.risk, db, exchange, quotes)
            commands = CommandListener(redis, kill_switch)
            # Before the bots load: a kill sent while the engine was down stops them first.
            await commands.drain()
            bots = await load_bots(db, exchange.exchange_id)
            if not bots:
                log.info("bots.none", hint="python -m engine.bots.create adds one")
            # dict.fromkeys drops repeats and keeps the order.
            bot_symbols = list(dict.fromkeys(bot.symbol for bot in bots))
            # Coins held by bots that aren't running too: equity values them at their bids,
            # and without a fresh price the stale data breaker blocks every buy (D42).
            # The kill switch sells them at those prices too (D43).
            held = await held_symbols(db, mode="paper")
            traded = list(dict.fromkeys([*bot_symbols, *held]))
            symbols = list(dict.fromkeys([*settings.price_symbols, *traded]))
            runner = BotRunner(bots, desk.risk, exchange, quotes, CandleStore(db), db)
            kill_switch.runner = runner
            # Before prices are fed, so no order left from before can fill first (D38).
            await runner.recover()
            # After that, as it cancelled their exits: they're placed again on the next price.
            await kill_switch.resume()

            async def on_price(update: PriceUpdate) -> None:
                # Each price, after its fills: the stops (D40), then the kill switch's sells.
                await runner.on_price(update)
                await kill_switch.on_price(update)

            # A TaskGroup runs the tasks side by side and waits for all of them to end.
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    heartbeat(
                        stop,
                        redis,
                        mode=settings.mode,
                        exchange=exchange.exchange_id,
                        symbols=symbols,
                        started_at=started_at,
                    )
                )
                workers = [
                    tasks.create_task(PriceStream(exchange, sink, symbol).run())
                    for symbol in symbols
                ]
                workers.append(tasks.create_task(runner.run()))
                workers.append(tasks.create_task(commands.run()))
                if traded:
                    workers.append(
                        tasks.create_task(
                            desk.feed_prices(redis, exchange.exchange_id, traded, then=on_price)
                        )
                    )
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
