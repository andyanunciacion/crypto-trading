"""Live prices: stream each symbol's ticker into Redis, and keep it flowing.

One `PriceStream` per symbol watches the exchange's WebSocket and writes every update
to Redis: the latest price under a key, plus a pub/sub message (layout in
shared/prices.py). When the stream fails, or goes quiet for `stale_after` seconds,
it drops the connection and reconnects after an exponential backoff. While it waits,
it polls the REST ticker, so prices stay fresh (the REST fallback, PRD §9).

Any error leads to a reconnect, never a crash: these are public prices, not orders,
so trying again is always safe. Individual updates aren't logged (up to 10 a second);
a stats line every minute shows the stream is alive.
"""

import asyncio
from datetime import UTC, datetime
from typing import Protocol

import structlog
from redis.asyncio import Redis

from engine.backoff import Backoff
from engine.exchange.models import Ticker
from shared.prices import PriceFeed, PriceUpdate, price_channel, price_key

# A liquid pair's best bid/ask changes many times a second, so 30 s of silence means
# the connection is dead even if it hasn't said so. A quiet small-cap pair may trip
# this too; that only costs a reconnect, and REST fills in meanwhile.
STALE_AFTER_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 2.0  # REST polling while the stream is down
STATS_INTERVAL_SECONDS = 60.0

log = structlog.get_logger()


class PriceSource(Protocol):
    """The part of ExchangeAdapter this module uses, so tests can pass a fake."""

    exchange_id: str

    async def watch_ticker(self, symbol: str) -> Ticker: ...

    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    async def close_stream(self, symbol: str) -> None: ...


class PriceSink(Protocol):
    async def publish(self, update: PriceUpdate) -> None: ...


class RedisPriceSink:
    """Stores each update as the symbol's latest price and publishes it on its channel."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, update: PriceUpdate) -> None:
        payload = update.model_dump_json()
        # One transaction (MULTI/EXEC): a reader never sees a key that disagrees with
        # the last message on the channel.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(price_key(update.exchange, update.symbol), payload)
            pipe.publish(price_channel(update.exchange, update.symbol), payload)
            await pipe.execute()


class PriceStream:
    def __init__(
        self,
        source: PriceSource,
        sink: PriceSink,
        symbol: str,
        *,
        backoff: Backoff | None = None,
        stale_after: float = STALE_AFTER_SECONDS,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        stats_interval: float = STATS_INTERVAL_SECONDS,
    ) -> None:
        self._source = source
        self._sink = sink
        self._symbol = symbol
        self._backoff = backoff or Backoff()
        self._stale_after = stale_after
        self._poll_interval = poll_interval
        self._stats_interval = stats_interval
        self._counts: dict[PriceFeed, int] = {"ws": 0, "rest": 0}
        self._stats_since = 0.0

    async def run(self) -> None:
        """Stream until cancelled (the engine cancels it on shutdown)."""
        log.info("prices.stream.starting", symbol=self._symbol, stale_after_s=self._stale_after)
        self._stats_since = asyncio.get_running_loop().time()
        try:
            while True:
                try:
                    await self._stream()
                except Exception as exc:
                    await self._recover(exc)
        finally:
            await self._source.close_stream(self._symbol)
            log.info("prices.stream.stopped", symbol=self._symbol)

    async def _stream(self) -> None:
        while True:
            async with asyncio.timeout(self._stale_after):
                ticker = await self._source.watch_ticker(self._symbol)
            await self._publish(ticker, "ws")
            if self._backoff.attempts:
                log.info(
                    "prices.stream.recovered", symbol=self._symbol, attempts=self._backoff.attempts
                )
                self._backoff.reset()

    async def _recover(self, exc: Exception) -> None:
        """Drop the dead connection, then poll REST until it's time to reconnect."""
        delay = self._backoff.next_delay()
        log.warning(
            "prices.stream.reconnecting",
            symbol=self._symbol,
            attempt=self._backoff.attempts,
            delay_s=round(delay, 2),
            # A timeout is ours: the watchdog found the stream silent.
            error="stale" if isinstance(exc, TimeoutError) else type(exc).__name__,
            detail=str(exc),
        )
        await self._source.close_stream(self._symbol)
        await self._poll_rest(delay)

    async def _poll_rest(self, duration: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        while (remaining := deadline - loop.time()) > 0:
            try:
                async with asyncio.timeout(remaining):
                    ticker = await self._source.fetch_ticker(self._symbol)
                    await self._publish(ticker, "rest")
            except TimeoutError:
                return  # time to reconnect
            except Exception as exc:
                log.info(
                    "prices.poll.failed",
                    symbol=self._symbol,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
            await asyncio.sleep(min(self._poll_interval, max(0.0, deadline - loop.time())))

    async def _publish(self, ticker: Ticker, source: PriceFeed) -> None:
        await self._sink.publish(
            PriceUpdate(
                exchange=self._source.exchange_id,
                symbol=ticker.symbol,
                bid=ticker.bid,
                ask=ticker.ask,
                last=ticker.last,
                bid_size=ticker.bid_size,
                ask_size=ticker.ask_size,
                ts=ticker.ts,
                received_at=datetime.now(UTC),
                source=source,
            )
        )
        self._counts[source] += 1
        now = asyncio.get_running_loop().time()
        if now - self._stats_since >= self._stats_interval:
            log.info(
                "prices.stream.stats",
                symbol=self._symbol,
                ws_updates=self._counts["ws"],
                rest_updates=self._counts["rest"],
                window_s=round(now - self._stats_since),
                bid=None if ticker.bid is None else str(ticker.bid),
                ask=None if ticker.ask is None else str(ticker.ask),
            )
            self._counts = {"ws": 0, "rest": 0}
            self._stats_since = now
