"""Backtest/live parity (P2-6): a recorded MEXC session gives the same signals both ways.

The recording (fixtures/mexc/live_btcusdt_1m.json.gz, made with
`python -m engine.marketdata.record_live`) holds what the live path saw: its warm-up
history, every kline update and REST reply in order, and the closed candles it got.
It also holds the same period fetched again afterwards, which is what a backtest of
that period replays.

- Live path: the recording played back through CandleStream into a SignalRunner. The
  position follows the signals (BUY opens, SELL closes), standing in for the broker
  until P3.
- Backtest path: run_backtest over the re-fetched candles.
"""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES_DIR

from engine.backtest.models import BacktestConfig, BacktestResult, default_slippage
from engine.backtest.simulator import run_backtest
from engine.exchange.errors import ExchangeNetworkError
from engine.exchange.models import Candle, MarketRules, Timeframe
from engine.marketdata.candle_stream import CandleStream
from engine.marketdata.record_live import (
    Failure,
    Fetch,
    Recording,
    RecordingSource,
    ReplayMismatch,
    ReplaySource,
    Watch,
    replay_stream,
)
from engine.marketdata.timeframes import STEP
from engine.strategies.base import Position, Signal, Strategy
from engine.strategies.bollinger_breakout import BollingerBreakout, BollingerBreakoutParams
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from engine.strategies.rsi_mean_reversion import RsiMeanReversion, RsiMeanReversionParams
from engine.strategies.runner import SignalRunner
from shared.backoff import Backoff

RECORDING = Recording.load(FIXTURES_DIR / "mexc/live_btcusdt_1m.json.gz")

STRATEGIES: list[Strategy[Any]] = [
    EmaCrossover(EmaCrossoverParams()),
    RsiMeanReversion(RsiMeanReversionParams()),
    BollingerBreakout(BollingerBreakoutParams()),
]


async def closed_candles(stream: CandleStream, n: int) -> list[Candle]:
    got: list[Candle] = []
    async with asyncio.timeout(10), aclosing(stream.closed()) as candles:
        async for candle in candles:
            got.append(candle)
            if len(got) == n:
                break
    return got


async def live_signals(strategy: Strategy[Any]) -> list[Signal]:
    runner = SignalRunner(strategy, RECORDING.symbol, RECORDING.timeframe)
    runner.seed(RECORDING.seed)
    position: Position | None = None
    signals = []
    for candle in await closed_candles(replay_stream(RECORDING), len(RECORDING.live)):
        signal = runner.on_closed(candle, position)
        if signal.action == "buy":
            position = Position(amount=Decimal(1), avg_entry=candle.close)
        elif signal.action == "sell":
            position = None
        if signal.action != "hold":
            signals.append(signal)
    return signals


def backtest(strategy: Strategy[Any]) -> BacktestResult:
    # A backtest also decides on its last warm-up candle, so that signal fills at the
    # first open in range. The live path decides only on candles it saw close, so the
    # range starts one candle in: both then decide on exactly the live candles.
    config = BacktestConfig(
        strategy=strategy.name,
        strategy_version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
        exchange=RECORDING.exchange,
        symbol=RECORDING.symbol,
        timeframe=RECORDING.timeframe,
        start=RECORDING.live[1].ts,
        end=RECORDING.live[-1].ts + STEP[RECORDING.timeframe],
        slippage_rate=default_slippage(RECORDING.symbol),
        market_rules=RECORDING.market_rules,
    )
    return run_backtest(strategy, RECORDING.reference, config)


async def test_live_path_gets_the_candles_a_backtest_replays() -> None:
    live = await closed_candles(replay_stream(RECORDING), len(RECORDING.live))

    assert live == RECORDING.live  # the replay reproduces the recorded session
    # What the live path saw is exactly what the backfill stores, seed included.
    assert RECORDING.seed + live == RECORDING.reference


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
async def test_live_and_backtest_signals_are_identical(strategy: Strategy[Any]) -> None:
    result = backtest(strategy)

    live = await live_signals(strategy)

    assert live, "no signals in the recording: the comparison would prove nothing"
    # Full Signal objects: action, candle, reason and the indicator values behind it.
    assert live == result.signals
    # The live position follows every signal, which matches the backtest only when
    # each signal filled. Only a signal on the very last candle may stay unfilled.
    assert all("last candle" in s.reason for s in result.skipped)


async def test_replay_refuses_a_live_path_that_asks_differently() -> None:
    # The parity test is only as good as the replay: a stream that makes other REST
    # requests than the recorded ones must fail loudly, not quietly pass.
    stream = replay_stream(RECORDING)
    stream._next -= STEP[RECORDING.timeframe]  # one candle further back than recorded

    with pytest.raises(ReplayMismatch, match="re-record"):
        await closed_candles(stream, 1)


# --- The recorder ------------------------------------------------------------------------

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def candle(i: int, close: int = 100) -> Candle:
    price = Decimal(close)
    return Candle(
        ts=T0 + i * MINUTE, open=price, high=price, low=price, close=price, volume=Decimal(1)
    )


@dataclass
class ScriptedExchange:
    stream: list[list[Candle] | Exception]
    exchange_id: str = "mexc"
    rest: list[Candle] = field(default_factory=lambda: [candle(i, 100 + i) for i in range(10)])

    async def watch_candles(self, symbol: str, timeframe: Timeframe) -> list[Candle]:
        item = self.stream.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fetch_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        assert since is not None and limit is not None
        return [c for c in self.rest if c.ts >= since][:limit]

    async def close_stream(self, symbol: str, timeframe: Timeframe | None = None) -> None:
        pass


def stream_over(source: Any, last_closed: int = 0) -> CandleStream:
    return CandleStream(
        source,
        "BTC/USDT",
        "1m",
        last_closed=T0 + last_closed * MINUTE,
        backoff=Backoff(base=0.0),
        settle=0.0,
        fetch_retry=0.0,
    )


async def test_a_recording_replays_to_the_same_candles(tmp_path: Path) -> None:
    down = ExchangeNetworkError("connection reset")
    script: list[list[Candle] | Exception] = [
        [candle(1)],
        [candle(1)],
        [candle(1)],
        [candle(2)],
        down,
        [candle(4)],
    ]
    source = RecordingSource(ScriptedExchange(script))
    live = await closed_candles(stream_over(source), 3)

    # Repeats of the same open candle are kept as a count.
    assert source.events == [
        Watch(candles=[candle(1)], repeat=3),
        Watch(candles=[candle(2)]),
        Fetch(since=T0 + MINUTE, limit=1, candles=[candle(1, 101)]),
        Failure(call="watch", error="ExchangeNetworkError", detail="connection reset"),
        Watch(candles=[candle(4)]),
        Fetch(since=T0 + 2 * MINUTE, limit=2, candles=[candle(2, 102), candle(3, 103)]),
    ]
    recording = Recording(
        exchange="mexc",
        symbol="BTC/USDT",
        timeframe="1m",
        recorded_at=T0,
        settle_seconds=0.0,
        fetch_tries=3,
        market_rules=MarketRules(
            symbol="BTC/USDT", amount_step=Decimal("0.00000001"), price_step=Decimal("0.01")
        ),
        seed=[candle(0, 100)],
        events=source.events,
        live=live,
        reference=[candle(i, 100 + i) for i in range(4)],
    )
    path = tmp_path / "session.json.gz"
    recording.save(path)
    loaded = Recording.load(path)

    assert loaded == recording
    assert await closed_candles(stream_over(ReplaySource(loaded)), 3) == live


async def test_replay_running_out_is_a_mismatch_not_a_hang() -> None:
    recording = Recording.load(FIXTURES_DIR / "mexc/live_btcusdt_1m.json.gz")

    with pytest.raises(ReplayMismatch, match="ran out"):
        await closed_candles(replay_stream(recording), len(recording.live) + 1)
