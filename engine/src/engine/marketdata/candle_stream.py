"""Live closed candles: the WebSocket says when one closed, REST supplies it (D33).

The exchange's kline stream sends the open candle many times a second. The first
update for a newer candle means every candle before it has closed. That's all the
stream is used for: the closed candles themselves are fetched over REST, the same
endpoint the backfill stores and backtests replay. A kline update can arrive a moment
before the close and miss the last trades; REST has the finished candle. So a live
bot and a backtest see the same prices, and the parity test (P2-6) checks that.

Closing is decided by the stream's own timestamps, never the wall clock, so a recorded
session replays exactly. A candle REST doesn't have yet is asked for again a few
times, then again when the next candle closes; one it never has is a gap, logged and
skipped, never made up (D25).

Like the price stream (prices.py), any error or 30 s of silence drops the connection
and reconnects after a backoff. Nothing is polled meanwhile: the first update after a
reconnect fetches every candle that closed in between.

An optional `on_kline` hook gets the newest candle of every update, open or not: the
dashboard's chart draws it as the candle still forming (D46). It's never traded on.
The hook can't disturb the stream: its errors are logged (once, until it works
again) and swallowed, and one that takes over `kline_timeout` is given up on.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime
from typing import Protocol

import structlog

from engine.backoff import Backoff
from engine.exchange.models import Candle, Timeframe
from engine.marketdata.timeframes import STEP

# MEXC returns at most 500 candles per request, however many are asked for.
PAGE_LIMIT = 500
# A busy pair's open candle updates every second or so; 30 s of silence means the
# connection is dead. A quiet pair may trip this too, which only costs a reconnect.
STALE_AFTER_SECONDS = 30.0
# Wait this long after a close before asking REST for the candle, so the exchange has
# finished it. Checked against a later re-fetch by the recorder (record_live.py).
SETTLE_SECONDS = 2.0
FETCH_TRIES = 3  # how often to ask for a candle REST doesn't have yet, per close
FETCH_RETRY_SECONDS = 2.0
# The on_kline hook writes to Redis, normally in about a millisecond.
KLINE_TIMEOUT_SECONDS = 1.0

log = structlog.get_logger()

# Called with each kline update's newest candle, which is usually still open.
KlineHook = Callable[[str, Timeframe, Candle], Awaitable[None]]


class CandleSource(Protocol):
    """The part of ExchangeAdapter this module uses, so tests can pass a fake."""

    exchange_id: str

    async def watch_candles(self, symbol: str, timeframe: Timeframe) -> list[Candle]: ...

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]: ...

    async def close_stream(self, symbol: str, timeframe: Timeframe | None = None) -> None: ...


async def fetch_range(
    source: CandleSource, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
) -> list[Candle]:
    """REST candles with open times in [start, end), paging past MEXC's 500 limit.

    Asks for exactly the candles in range, so an open candle at `end` isn't included.
    """
    step = STEP[timeframe]
    got: list[Candle] = []
    since = start
    while since < end:
        limit = min((end - since) // step, PAGE_LIMIT)
        page = await source.fetch_candles(symbol, timeframe, since=since, limit=limit)
        wanted = [c for c in page if since <= c.ts < end]
        got += wanted
        # Nothing in range: the exchange has no candles there (yet), so move on.
        since = wanted[-1].ts + step if wanted else since + limit * step
    return got


class CandleStream:
    """Closed candles of one symbol and timeframe, each once, in order, after `last_closed`.

    `last_closed` is the open time of the newest candle the caller already has (its
    warm-up history). Consume with `async with contextlib.aclosing(stream.closed())`,
    so leaving the loop closes the socket straight away.
    """

    def __init__(
        self,
        source: CandleSource,
        symbol: str,
        timeframe: Timeframe,
        last_closed: datetime,
        *,
        backoff: Backoff | None = None,
        stale_after: float = STALE_AFTER_SECONDS,
        settle: float = SETTLE_SECONDS,
        fetch_tries: int = FETCH_TRIES,
        fetch_retry: float = FETCH_RETRY_SECONDS,
        on_kline: KlineHook | None = None,
        kline_timeout: float = KLINE_TIMEOUT_SECONDS,
    ) -> None:
        self._source = source
        self._symbol = symbol
        self._timeframe = timeframe
        self._step = STEP[timeframe]
        self._next = last_closed + self._step  # the oldest candle not delivered yet
        self._backoff = backoff or Backoff()
        self._stale_after = stale_after
        self._settle = settle
        self._fetch_tries = fetch_tries
        self._fetch_retry = fetch_retry
        # The open candle REST was last asked about: not again until a newer one opens.
        self._asked_at: datetime | None = None
        self._on_kline = on_kline
        self._kline_timeout = kline_timeout
        self._kline_failing = False  # logged once when the hook starts failing

    async def closed(self) -> AsyncGenerator[Candle]:
        log.info(
            "candles.live.starting",
            symbol=self._symbol,
            timeframe=self._timeframe,
            next=self._next.isoformat(),
        )
        try:
            while True:
                try:
                    newest = await self._newest_open()
                except Exception as exc:
                    await self._recover(exc)
                    continue
                if newest is None or newest <= self._next or newest == self._asked_at:
                    continue
                try:
                    candles = await self._fetch_closed(newest)
                except Exception as exc:
                    # The adapter already retried network errors; the next update tries again.
                    log.warning(
                        "candles.live.fetch_failed",
                        symbol=self._symbol,
                        timeframe=self._timeframe,
                        error=type(exc).__name__,
                        detail=str(exc),
                    )
                    continue
                self._asked_at = newest
                for candle in candles:
                    self._deliver(candle)
                    yield candle
        finally:
            await self._source.close_stream(self._symbol, self._timeframe)
            log.info("candles.live.stopped", symbol=self._symbol, timeframe=self._timeframe)

    async def _newest_open(self) -> datetime | None:
        """Wait for the next kline update; the newest candle in it is (usually) still open."""
        async with asyncio.timeout(self._stale_after):
            updates = await self._source.watch_candles(self._symbol, self._timeframe)
        if self._backoff.attempts:
            log.info(
                "candles.live.recovered",
                symbol=self._symbol,
                timeframe=self._timeframe,
                attempts=self._backoff.attempts,
            )
            self._backoff.reset()
        newest = max(updates, key=lambda c: c.ts, default=None)
        if newest is not None and self._on_kline is not None:
            await self._kline(self._on_kline, newest)
        return None if newest is None else newest.ts

    async def _kline(self, hook: KlineHook, candle: Candle) -> None:
        try:
            async with asyncio.timeout(self._kline_timeout):
                await hook(self._symbol, self._timeframe, candle)
        except Exception as exc:
            if not self._kline_failing:
                self._kline_failing = True
                log.warning(
                    "candles.forming.failed",
                    symbol=self._symbol,
                    timeframe=self._timeframe,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
            return
        if self._kline_failing:
            self._kline_failing = False
            log.info("candles.forming.recovered", symbol=self._symbol, timeframe=self._timeframe)

    async def _recover(self, exc: Exception) -> None:
        delay = self._backoff.next_delay()
        log.warning(
            "candles.live.reconnecting",
            symbol=self._symbol,
            timeframe=self._timeframe,
            attempt=self._backoff.attempts,
            delay_s=round(delay, 2),
            # A timeout is ours: the watchdog found the stream silent.
            error="stale" if isinstance(exc, TimeoutError) else type(exc).__name__,
            detail=str(exc),
        )
        await self._source.close_stream(self._symbol, self._timeframe)
        await asyncio.sleep(delay)

    async def _fetch_closed(self, newest: datetime) -> list[Candle]:
        """The candles from the next undelivered one up to `newest` (open), from REST."""
        await asyncio.sleep(self._settle)
        last = newest - self._step  # the candle that has just closed
        for attempt in range(1, self._fetch_tries + 1):
            candles = await fetch_range(
                self._source, self._symbol, self._timeframe, self._next, newest
            )
            if candles and candles[-1].ts == last:
                return candles
            if attempt < self._fetch_tries:
                log.info(
                    "candles.live.not_ready",
                    symbol=self._symbol,
                    timeframe=self._timeframe,
                    ts=last.isoformat(),
                    attempt=attempt,
                )
                await asyncio.sleep(self._fetch_retry)
        log.warning(
            "candles.live.late",
            symbol=self._symbol,
            timeframe=self._timeframe,
            ts=last.isoformat(),
            tries=self._fetch_tries,
        )
        return candles  # what there is; the rest is asked for at the next close

    def _deliver(self, candle: Candle) -> None:
        if candle.ts != self._next:
            log.warning(
                "candles.live.gap",
                symbol=self._symbol,
                timeframe=self._timeframe,
                start=self._next.isoformat(),
                end=candle.ts.isoformat(),
                missing=(candle.ts - self._next) // self._step,
            )
        log.info(
            "candles.live.closed",
            symbol=self._symbol,
            timeframe=self._timeframe,
            ts=candle.ts.isoformat(),
            close=str(candle.close),
        )
        self._next = candle.ts + self._step
