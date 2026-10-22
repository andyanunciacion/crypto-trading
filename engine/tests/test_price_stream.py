"""The price stream: publishing, reconnecting with backoff, the REST fallback.

A fake exchange plays a script of stream events (tickers, errors, silence), and an
in-memory sink records what would go to Redis. Timings are shrunk to milliseconds,
so no test waits for real backoff. test_redis_sink.py covers the Redis side.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from engine.backoff import Backoff
from engine.exchange.errors import ExchangeError, ExchangeNetworkError
from engine.exchange.models import Ticker
from engine.marketdata.prices import PriceStream
from shared.prices import PriceUpdate

BTC = "BTC/USDT"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


class Silence:
    """In a script: the stream sends nothing more, like a socket after the network is pulled."""


def ticker(bid: str = "81819.48", ask: str = "81819.49", last: str | None = None) -> Ticker:
    return Ticker(
        symbol=BTC,
        ts=T0,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=None if last is None else Decimal(last),
    )


@dataclass
class FakeExchange:
    """Plays `stream` for watch_ticker, one item per call; then stays silent."""

    stream: list[Ticker | Exception | Silence]
    rest: Ticker | Exception = field(default_factory=lambda: ticker(last="81820.01"))
    exchange_id: str = "mexc"
    watches: int = 0
    polls: int = 0
    closed: list[str] = field(default_factory=list)

    async def watch_ticker(self, symbol: str) -> Ticker:
        assert symbol == BTC
        self.watches += 1
        item = self.stream.pop(0) if self.stream else Silence()
        if isinstance(item, Silence):
            await asyncio.Event().wait()  # never set: waits until cancelled
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, Ticker)
        return item

    async def fetch_ticker(self, symbol: str) -> Ticker:
        assert symbol == BTC
        self.polls += 1
        if isinstance(self.rest, Exception):
            raise self.rest
        return self.rest

    async def close_stream(self, symbol: str) -> None:
        self.closed.append(symbol)


@dataclass
class MemorySink:
    updates: list[PriceUpdate] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)  # raised first, one per call

    async def publish(self, update: PriceUpdate) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.updates.append(update)

    def by(self, source: str) -> list[PriceUpdate]:
        return [u for u in self.updates if u.source == source]


class RecordingBackoff(Backoff):
    """A fixed wait per failure, so tests control how long the REST fallback runs."""

    def __init__(self, delay: float = 0.001) -> None:
        super().__init__()
        self.delay = delay
        self.delays: list[float] = []

    def next_delay(self) -> float:
        self.attempts += 1
        self.delays.append(self.delay)
        return self.delay


def make_stream(
    exchange: FakeExchange,
    sink: MemorySink,
    backoff: Backoff | None = None,
    *,
    stale_after: float = 1.0,
) -> PriceStream:
    return PriceStream(
        exchange,
        sink,
        BTC,
        backoff=backoff or RecordingBackoff(),
        stale_after=stale_after,
        poll_interval=0.005,
    )


async def run_until(stream: PriceStream, done: Callable[[], bool]) -> None:
    """Run the stream until `done()` is true, then stop it as the engine does (cancel)."""
    task = asyncio.create_task(stream.run())
    try:
        async with asyncio.timeout(3):  # only guards against a hang
            while not done():  # noqa: ASYNC110 (polling test state, not an event)
                await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_stream_updates_are_published() -> None:
    exchange = FakeExchange([ticker(), ticker(bid="81819.50", ask="81819.51")])
    sink = MemorySink()

    await run_until(make_stream(exchange, sink), lambda: len(sink.updates) == 2)

    first, second = sink.updates
    assert first.exchange == "mexc"
    assert first.symbol == BTC
    assert first.source == "ws"
    assert (first.bid, first.ask, first.last) == (Decimal("81819.48"), Decimal("81819.49"), None)
    assert first.ts == T0
    assert first.received_at.tzinfo is not None
    assert second.bid == Decimal("81819.50")
    assert exchange.closed == [BTC]  # stopping closes the socket


async def test_network_errors_reconnect_with_growing_backoff_then_reset() -> None:
    down = ExchangeNetworkError("connection reset")
    exchange = FakeExchange([down, down, down, ticker()])
    sink = MemorySink()
    backoff = RecordingBackoff()

    await run_until(make_stream(exchange, sink, backoff), lambda: len(sink.by("ws")) == 1)

    assert len(backoff.delays) == 3  # one wait per failure
    assert exchange.closed[:3] == [BTC, BTC, BTC]  # each dead socket is dropped first
    assert exchange.stream == []  # it reconnected after each failure
    assert backoff.attempts == 0  # the first good update resets it


async def test_a_silent_stream_counts_as_dead() -> None:
    # The pulled-network case: the socket raises nothing, it just goes quiet.
    exchange = FakeExchange([ticker(), Silence(), ticker(bid="81819.50", ask="81819.51")])
    sink = MemorySink()

    await run_until(make_stream(exchange, sink, stale_after=0.05), lambda: len(sink.by("ws")) == 2)

    assert exchange.closed[0] == BTC
    assert exchange.stream == []


async def test_rest_keeps_prices_fresh_while_the_stream_is_down() -> None:
    exchange = FakeExchange([ExchangeNetworkError("down")])  # then silent
    sink = MemorySink()

    await run_until(
        make_stream(exchange, sink, RecordingBackoff(delay=0.2)),
        lambda: len(sink.by("rest")) >= 2,
    )

    update = sink.by("rest")[0]
    assert update.last == Decimal("81820.01")  # REST also has the last trade
    assert update.bid == Decimal("81819.48")


async def test_rest_failing_too_is_survived() -> None:
    # Network pulled: the stream and REST both fail until it comes back.
    down = ExchangeNetworkError("down")
    exchange = FakeExchange([down, down, ticker()], rest=ExchangeNetworkError("down"))
    sink = MemorySink()

    await run_until(
        make_stream(exchange, sink, RecordingBackoff(delay=0.02)),
        lambda: len(sink.by("ws")) == 1,
    )

    assert exchange.polls > 0
    assert sink.by("rest") == []


@pytest.mark.parametrize(
    "error",
    [ExchangeError("unexpected Ticker data"), ValueError("a bug"), ConnectionError("redis")],
    ids=["bad-data", "unexpected", "other"],
)
async def test_any_error_reconnects_instead_of_crashing(error: Exception) -> None:
    exchange = FakeExchange([error, ticker()])
    sink = MemorySink()

    await run_until(make_stream(exchange, sink), lambda: len(sink.by("ws")) == 1)

    assert exchange.closed[0] == BTC


async def test_a_failed_write_to_redis_is_survived() -> None:
    exchange = FakeExchange([ticker(), ticker(bid="81819.50", ask="81819.51")])
    sink = MemorySink(failures=[ConnectionError("redis is down")])

    await run_until(make_stream(exchange, sink), lambda: len(sink.by("ws")) == 1)

    assert sink.by("ws")[0].bid == Decimal("81819.50")
