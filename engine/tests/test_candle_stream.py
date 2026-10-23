"""The live candle stream: WebSocket updates say when a candle closed, REST supplies it.

A fake exchange plays a script of kline updates (and errors, and silence) and answers
REST requests from a list of "exchange" candles, some of which can show up late or
never. Waits are shrunk to nothing, so no test sleeps for real.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from engine.backoff import Backoff
from engine.exchange.errors import ExchangeNetworkError
from engine.exchange.models import Candle, Timeframe
from engine.marketdata.candle_stream import PAGE_LIMIT, CandleStream

BTC = "BTC/USDT"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def at(i: int) -> datetime:
    return T0 + i * MINUTE


def rest_candle(i: int) -> Candle:
    """Candle `i` as REST has it once closed: the close is 100 + i."""
    close = Decimal(100 + i)
    return Candle(ts=at(i), open=close, high=close, low=close, close=close, volume=Decimal(5))


def update(i: int) -> list[Candle]:
    """A WebSocket kline update for candle `i`, still open: its prices aren't final."""
    return [
        Candle(
            ts=at(i),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(0),
        )
    ]


class Silence:
    """In a script: the stream sends nothing more, like a socket after the network is pulled."""


@dataclass
class FakeExchange:
    stream: list[list[Candle] | Exception | Silence]
    rest: list[Candle] = field(default_factory=lambda: [rest_candle(i) for i in range(-5, 2000)])
    # Candle index -> how many more REST requests it stays missing from (REST lag).
    late: dict[int, int] = field(default_factory=dict)
    rest_errors: list[Exception] = field(default_factory=list)  # raised first, one per request
    exchange_id: str = "mexc"
    requests: list[tuple[datetime | None, int | None]] = field(default_factory=list)
    closed: list[tuple[str, Timeframe | None]] = field(default_factory=list)

    async def watch_candles(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        assert (symbol, timeframe) == (BTC, "1m")
        item = self.stream.pop(0) if self.stream else Silence()
        if isinstance(item, Silence):
            await asyncio.Event().wait()  # never set: waits until cancelled
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, list)
        return item

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        assert (symbol, timeframe) == (BTC, "1m")
        self.requests.append((since, limit))
        if self.rest_errors:
            raise self.rest_errors.pop(0)
        hidden = set()
        for i, remaining in self.late.items():
            if remaining > 0:
                hidden.add(at(i))
                self.late[i] = remaining - 1
        rows = [c for c in self.rest if (since is None or c.ts >= since) and c.ts not in hidden]
        return rows[: min(limit or PAGE_LIMIT, PAGE_LIMIT)]

    async def close_stream(self, symbol: str, timeframe: Timeframe | None = None) -> None:
        self.closed.append((symbol, timeframe))


class RecordingBackoff(Backoff):
    def __init__(self) -> None:
        super().__init__()
        self.delays: list[float] = []

    def next_delay(self) -> float:
        self.attempts += 1
        self.delays.append(0.0)
        return 0.0


def make_stream(
    exchange: FakeExchange,
    last_closed: int = 0,
    *,
    backoff: Backoff | None = None,
    stale_after: float = 1.0,
) -> CandleStream:
    return CandleStream(
        exchange,
        BTC,
        "1m",
        last_closed=at(last_closed),
        backoff=backoff or RecordingBackoff(),
        stale_after=stale_after,
        settle=0.0,
        fetch_retry=0.0,
    )


async def take(stream: CandleStream, n: int) -> list[Candle]:
    """The first `n` closed candles, then stop the stream as a consumer would."""
    got: list[Candle] = []
    async with asyncio.timeout(3):  # only guards against a hang
        candles: AsyncGenerator[Candle]
        async with aclosing(stream.closed()) as candles:
            async for candle in candles:
                got.append(candle)
                if len(got) == n:
                    break
    return got


async def test_each_closed_candle_comes_from_rest_once_in_order() -> None:
    # Several updates per candle; the first update of candle 2 closes candle 1.
    exchange = FakeExchange([update(1), update(1), update(1), update(2), update(2), update(3)])

    got = await take(make_stream(exchange), 2)

    assert got == [rest_candle(1), rest_candle(2)]  # REST's prices, not the open update's
    assert exchange.requests == [(at(1), 1), (at(2), 1)]


async def test_an_older_update_is_ignored() -> None:
    exchange = FakeExchange([update(2), update(1), update(2), update(3)])

    got = await take(make_stream(exchange), 2)

    assert got == [rest_candle(1), rest_candle(2)]


async def test_catches_up_every_candle_missed_while_disconnected() -> None:
    down = ExchangeNetworkError("connection reset")
    exchange = FakeExchange([update(1), down, update(5)])

    got = await take(make_stream(exchange), 4)

    assert got == [rest_candle(i) for i in range(1, 5)]
    assert exchange.requests == [(at(1), 4)]  # one request for all four
    assert exchange.closed[0] == (BTC, "1m")  # the dead socket was dropped


async def test_catch_up_pages_through_a_long_outage() -> None:
    exchange = FakeExchange([update(PAGE_LIMIT + 101)])

    got = await take(make_stream(exchange), PAGE_LIMIT + 100)

    assert got == [rest_candle(i) for i in range(1, PAGE_LIMIT + 101)]
    assert exchange.requests == [(at(1), PAGE_LIMIT), (at(PAGE_LIMIT + 1), 100)]


async def test_starts_from_a_seed_that_is_behind() -> None:
    # The engine was down: the seed ends at candle 0, the market is at candle 3.
    exchange = FakeExchange([update(3)])

    got = await take(make_stream(exchange, last_closed=0), 2)

    assert got == [rest_candle(1), rest_candle(2)]


async def test_a_candle_late_on_rest_is_asked_for_again() -> None:
    exchange = FakeExchange([update(2)], late={1: 1})

    with capture_logs() as logs:
        got = await take(make_stream(exchange), 1)

    assert got == [rest_candle(1)]
    assert exchange.requests == [(at(1), 1), (at(1), 1)]
    assert [e["event"] for e in logs].count("candles.live.not_ready") == 1


async def test_a_candle_still_missing_waits_for_the_next_close() -> None:
    # Missing after every try: it's asked for again when candle 3 opens, not on every update.
    exchange = FakeExchange([update(2), update(2), update(3)], late={1: 3})

    got = await take(make_stream(exchange), 2)

    assert got == [rest_candle(1), rest_candle(2)]
    assert exchange.requests == [(at(1), 1)] * 3 + [(at(1), 2)]


async def test_a_gap_is_logged_and_never_filled() -> None:
    # The exchange has no candle 2 at all (D25): nothing is made up for it.
    exchange = FakeExchange(
        [update(2), update(3), update(4)],
        rest=[rest_candle(i) for i in range(10) if i != 2],
    )

    with capture_logs() as logs:
        got = await take(make_stream(exchange), 2)

    assert got == [rest_candle(1), rest_candle(3)]
    (gap,) = [e for e in logs if e["event"] == "candles.live.gap"]
    assert gap["log_level"] == "warning"
    assert gap["start"] == at(2).isoformat()
    assert gap["missing"] == 1


async def test_a_failed_rest_request_is_retried_on_the_next_update() -> None:
    exchange = FakeExchange(
        [update(2), update(2)], rest_errors=[ExchangeNetworkError("gave up after 5 tries")]
    )

    with capture_logs() as logs:
        got = await take(make_stream(exchange), 1)

    assert got == [rest_candle(1)]
    (failed,) = [e for e in logs if e["event"] == "candles.live.fetch_failed"]
    assert failed["error"] == "ExchangeNetworkError"


async def test_a_silent_stream_counts_as_dead() -> None:
    exchange = FakeExchange([update(1), Silence(), update(2)])

    got = await take(make_stream(exchange, stale_after=0.05), 1)

    assert got == [rest_candle(1)]
    assert exchange.closed[0] == (BTC, "1m")


async def test_reconnects_are_logged_and_the_backoff_resets() -> None:
    down = ExchangeNetworkError("connection reset")
    exchange = FakeExchange([down, down, update(2)])
    backoff = RecordingBackoff()

    with capture_logs() as logs:
        await take(make_stream(exchange, backoff=backoff), 1)

    reconnects = [e for e in logs if e["event"] == "candles.live.reconnecting"]
    assert [e["attempt"] for e in reconnects] == [1, 2]
    assert reconnects[0]["log_level"] == "warning"
    (recovered,) = [e for e in logs if e["event"] == "candles.live.recovered"]
    assert recovered["attempts"] == 2
    assert backoff.attempts == 0


@pytest.mark.parametrize("error", [ValueError("a bug"), ExchangeNetworkError("down")])
async def test_any_stream_error_reconnects_instead_of_crashing(error: Exception) -> None:
    exchange = FakeExchange([error, update(2)])

    got = await take(make_stream(exchange), 1)

    assert got == [rest_candle(1)]


async def test_every_closed_candle_is_logged() -> None:
    exchange = FakeExchange([update(2)])

    with capture_logs() as logs:
        await take(make_stream(exchange), 1)

    (closed,) = [e for e in logs if e["event"] == "candles.live.closed"]
    assert closed["ts"] == at(1).isoformat()
    assert closed["close"] == "101"


async def test_stopping_closes_the_socket() -> None:
    exchange = FakeExchange([update(2)])

    await take(make_stream(exchange), 1)

    assert exchange.closed == [(BTC, "1m")]
