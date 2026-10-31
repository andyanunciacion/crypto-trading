"""Record a live candle session for the backtest/live parity test (P2-6, D33).

Run it in Docker, with the fixtures folder mounted (about 3 hours for 180 1m candles):
    docker compose run --rm -v ${PWD}/engine/tests/fixtures:/fixtures engine
        python -m engine.marketdata.record_live BTC/USDT 1m --candles 180
        --out /fixtures/mexc/live_btcusdt_1m.json.gz

1. Fetches `--warmup` closed candles over REST: what a live bot starts from.
2. Runs the real CandleStream through a recording proxy, which keeps every kline update
   and every REST request and reply, until `--candles` candles have closed.
3. Waits `--recheck-after` seconds, then fetches the whole period again, as the
   backfill would: what a backtest of that period replays.
4. Writes it all as gzipped JSON, and prints every candle where what the live path got
   at the close differs from the re-fetch (exit code 1 if any), which would mean
   SETTLE_SECONDS is too short.

`ReplaySource` plays a recording back through the same CandleStream, offline.
"""

import argparse
import asyncio
import gzip
import sys
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, get_args

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from engine.exchange.adapter import ExchangeAdapter
from engine.exchange.errors import ExchangeNetworkError
from engine.exchange.models import Candle, MarketRules, Timeframe
from engine.marketdata.candle_stream import (
    FETCH_TRIES,
    SETTLE_SECONDS,
    CandleSource,
    CandleStream,
    fetch_range,
)
from engine.marketdata.timeframes import STEP, floor
from shared.backoff import Backoff
from shared.config import load_settings
from shared.logs import configure_logging

log = structlog.get_logger()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Watch(_Frozen):
    """A kline update, or `repeat` in a row whose newest candle is the same.

    The live path only reads the newest candle's open time from an update, and a busy
    pair sends several a second, so repeats are kept as a count, not one by one.
    """

    kind: Literal["watch"] = "watch"
    candles: list[Candle]
    repeat: int = Field(default=1, ge=1)


class Fetch(_Frozen):
    kind: Literal["fetch"] = "fetch"
    since: AwareDatetime | None
    limit: int | None
    candles: list[Candle]


class Failure(_Frozen):
    """A watch or fetch that raised; `error` is "stale" when the watchdog timed out."""

    kind: Literal["failure"] = "failure"
    call: Literal["watch", "fetch"]
    error: str
    detail: str


Event = Annotated[Watch | Fetch | Failure, Field(discriminator="kind")]


class Recording(_Frozen):
    exchange: str
    symbol: str
    timeframe: Timeframe
    recorded_at: AwareDatetime
    settle_seconds: float
    fetch_tries: int
    market_rules: MarketRules
    seed: list[Candle]  # the warm-up history the live path started from
    events: list[Event]  # everything the live path saw, in order
    live: list[Candle]  # the closed candles it delivered
    reference: list[Candle]  # seed + live's period, fetched again afterwards

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # mtime=0: the same recording always gives the same bytes.
        with gzip.GzipFile(path, "wb", mtime=0) as f:
            f.write(self.model_dump_json(indent=1).encode("utf-8"))

    @classmethod
    def load(cls, path: Path) -> "Recording":
        with gzip.open(path, "rb") as f:
            return cls.model_validate_json(f.read())


class RecordingSource:
    """Passes calls on to the exchange and keeps a record of each one."""

    def __init__(self, source: CandleSource) -> None:
        self._source = source
        self.exchange_id = source.exchange_id
        self.events: list[Watch | Fetch | Failure] = []

    async def watch_candles(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        try:
            candles = await self._source.watch_candles(symbol, timeframe)
        except asyncio.CancelledError:
            # How the stream's silence watchdog stops a watch (asyncio.timeout).
            self.events.append(Failure(call="watch", error="stale", detail="no update"))
            raise
        except Exception as exc:
            self.events.append(Failure(call="watch", error=type(exc).__name__, detail=str(exc)))
            raise
        last = self.events[-1] if self.events else None
        if isinstance(last, Watch) and _newest(last.candles) == _newest(candles):
            self.events[-1] = last.model_copy(update={"repeat": last.repeat + 1})
        else:
            self.events.append(Watch(candles=candles))
        return candles

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        try:
            candles = await self._source.fetch_candles(symbol, timeframe, since, limit)
        except Exception as exc:
            self.events.append(Failure(call="fetch", error=type(exc).__name__, detail=str(exc)))
            raise
        self.events.append(Fetch(since=since, limit=limit, candles=candles))
        return candles

    async def close_stream(self, symbol: str, timeframe: Timeframe | None = None) -> None:
        await self._source.close_stream(symbol, timeframe)


class ReplayMismatch(BaseException):
    """The live path asked for something the recording doesn't have next.

    A BaseException, so the stream's catch-all reconnect doesn't swallow it.
    """


class ReplaySource:
    """Plays a recording back: each call must be the one recorded next."""

    def __init__(self, recording: Recording) -> None:
        self.exchange_id = recording.exchange
        self._symbol = recording.symbol
        self._timeframe = recording.timeframe
        self._events = list(recording.events)

    async def watch_candles(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        self._check(symbol, timeframe)
        event = self._next("watch")
        if isinstance(event, Failure):
            if event.error == "stale":
                raise TimeoutError(event.detail)  # as the watchdog's timeout would
            raise ExchangeNetworkError(f"{event.error}: {event.detail}")
        assert isinstance(event, Watch)
        return event.candles

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        self._check(symbol, timeframe)
        event = self._next("fetch")
        if isinstance(event, Failure):
            raise ExchangeNetworkError(f"{event.error}: {event.detail}")
        assert isinstance(event, Fetch)
        if (since, limit) != (event.since, event.limit):
            raise ReplayMismatch(
                f"live path asked REST for since={since}, limit={limit}; the recording has "
                f"since={event.since}, limit={event.limit}. Its REST calls changed: re-record"
            )
        return event.candles

    async def close_stream(self, symbol: str, timeframe: Timeframe | None = None) -> None:
        pass

    def _check(self, symbol: str, timeframe: Timeframe) -> None:
        if (symbol, timeframe) != (self._symbol, self._timeframe):
            raise ReplayMismatch(f"recorded {self._symbol} {self._timeframe}, not {symbol}")

    def _next(self, call: Literal["watch", "fetch"]) -> Watch | Fetch | Failure:
        if not self._events:
            raise ReplayMismatch(f"the recording ran out; the live path wanted a {call}")
        event = self._events[0]
        recorded = event.call if isinstance(event, Failure) else event.kind
        if recorded != call:
            raise ReplayMismatch(
                f"the live path wanted a {call}; the recording has a {recorded}. "
                "Its calls changed: re-record"
            )
        if isinstance(event, Watch) and event.repeat > 1:
            self._events[0] = event.model_copy(update={"repeat": event.repeat - 1})
        else:
            self._events.pop(0)
        return event


def _newest(candles: list[Candle]) -> datetime | None:
    return max((c.ts for c in candles), default=None)


def replay_stream(recording: Recording) -> CandleStream:
    """The live candle stream over `recording`, with every wait shrunk to nothing."""
    return CandleStream(
        ReplaySource(recording),
        recording.symbol,
        recording.timeframe,
        last_closed=recording.seed[-1].ts,
        backoff=Backoff(base=0.0),
        stale_after=60.0,  # replies come at once; this only guards against a hang
        settle=0.0,
        fetch_tries=recording.fetch_tries,
        fetch_retry=0.0,
    )


def differences(recording: Recording) -> list[str]:
    """Where the live path's candles (seed and live) differ from the later re-fetch."""
    reference = {c.ts: c for c in recording.reference}
    problems = []
    for candle in recording.seed + recording.live:
        again = reference.get(candle.ts)
        if again is None:
            problems.append(f"{candle.ts:%Y-%m-%d %H:%M} missing from the re-fetch")
        elif again != candle:
            problems.append(
                f"{candle.ts:%Y-%m-%d %H:%M} live {_ohlcv(candle)} later {_ohlcv(again)}"
            )
    return problems


def _ohlcv(c: Candle) -> str:
    return f"o={c.open} h={c.high} l={c.low} c={c.close} v={c.volume}"


async def record(args: argparse.Namespace) -> Recording:
    step = STEP[args.timeframe]
    # Public market data only: no key, trading off (D22).
    async with ExchangeAdapter(args.exchange) as exchange:
        rules = await exchange.market_rules(args.symbol)
        now = datetime.now(UTC)
        open_ts = floor(now, args.timeframe)
        seed = await fetch_range(
            exchange, args.symbol, args.timeframe, open_ts - args.warmup * step, open_ts
        )
        if not seed:
            raise SystemExit(f"no {args.timeframe} candles for {args.symbol}")
        source = RecordingSource(exchange)
        stream = CandleStream(source, args.symbol, args.timeframe, last_closed=seed[-1].ts)
        live: list[Candle] = []
        async with aclosing(stream.closed()) as candles:
            async for candle in candles:
                live.append(candle)
                print(f"{len(live)}/{args.candles} closed: {candle.ts:%H:%M} UTC", file=sys.stderr)
                if len(live) == args.candles:
                    break
        print(f"waiting {args.recheck_after} s before the re-fetch", file=sys.stderr)
        await asyncio.sleep(args.recheck_after)
        reference = await fetch_range(
            exchange, args.symbol, args.timeframe, seed[0].ts, live[-1].ts + step
        )
    return Recording(
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        recorded_at=now,
        settle_seconds=SETTLE_SECONDS,
        fetch_tries=FETCH_TRIES,
        market_rules=rules,
        seed=seed,
        events=source.events,
        live=live,
        reference=reference,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("symbol", help="CCXT format, e.g. BTC/USDT")
    parser.add_argument("timeframe", choices=get_args(Timeframe))
    parser.add_argument("--candles", type=int, default=180, help="closed candles to record")
    parser.add_argument("--warmup", type=int, default=300, help="REST history to start from")
    parser.add_argument(
        "--recheck-after", type=float, default=120.0, help="seconds before the re-fetch"
    )
    parser.add_argument("--out", type=Path, required=True, help="where to write (.json.gz)")
    parser.add_argument("--exchange", default="mexc")
    args = parser.parse_args()

    # stdout stays free for the summary; the JSON logs go to stderr.
    configure_logging(load_settings().log_level, stream=sys.stderr)
    recording = asyncio.run(record(args))
    recording.save(args.out)
    watches = sum(e.repeat for e in recording.events if isinstance(e, Watch))
    fetches = sum(isinstance(e, Fetch) for e in recording.events)
    failures = [e for e in recording.events if isinstance(e, Failure)]
    print(
        f"wrote {args.out}: {len(recording.seed)} seed + {len(recording.live)} live candles, "
        f"{watches} kline updates, {fetches} REST requests, {len(failures)} failures"
    )
    problems = differences(recording)
    for problem in problems:
        print(f"  differs: {problem}")
    print(f"{len(problems)} candle(s) differ from the re-fetch")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
