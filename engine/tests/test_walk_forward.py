"""Walk-forward: pick params on one window, test them on the next, roll forward."""

import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import URL, text

from engine.backtest.models import BacktestConfig
from engine.backtest.run import reproduce
from engine.backtest.simulator import BacktestError
from engine.backtest.store import BacktestRunStore
from engine.backtest.sweep import param_grid, sweep
from engine.backtest.walk_forward import (
    Window,
    compounded,
    describe,
    mean_sharpe,
    run_walk_forward,
    walk_forward,
    windows,
)
from engine.exchange.models import Candle, MarketRules
from engine.marketdata.store import CandleStore, Series
from engine.strategies.ema_crossover import EmaCrossover
from shared.db.session import make_engine

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
D = Decimal

RULES = MarketRules(
    symbol="BTC/USDT",
    amount_step=D("0.000001"),
    price_step=D("0.01"),
    min_amount=D("0.000001"),
    min_cost=D("1"),
)
GRID = param_grid(EmaCrossover, {"fast": [3, 5], "slow": [8, 13, 21]})  # warm-ups up to 64


def at(hour: int) -> datetime:
    return T0 + hour * HOUR


def wave(hour: int, shift: int = 0) -> Candle:
    # A slow sine wave with a fast wobble on top, so the EMAs cross several times.
    h = hour + shift
    close = round(30000 + 2000 * math.sin(h / 20) + 150 * math.sin(h), 2)
    open_ = round(close * (1 + 0.001 * math.cos(h)), 2)
    o, c = D(str(open_)), D(str(close))
    return Candle(
        ts=at(hour), open=o, high=max(o, c) + 10, low=min(o, c) - 10, close=c, volume=D(1)
    )


HISTORY = [wave(h) for h in range(1000)]
# Train 200 candles, test 100: six windows between candle 200 and candle 1000.
WINDOWS = windows(at(200), at(1000), 200 * HOUR, 100 * HOUR, "1h")


def base(**overrides: Any) -> BacktestConfig:
    values: dict[str, Any] = {
        "strategy": "placeholder",
        "strategy_version": 1,
        "params": {},
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start": at(200),
        "end": at(1000),
        "initial_cash": D(1000),
        "fee_rate": D("0.001"),
        "slippage_rate": D("0.0005"),
        "market_rules": RULES,
    }
    return BacktestConfig(**(values | overrides))


# --- Windows ----------------------------------------------------------------------------


def test_windows_roll_forward_by_the_test_length() -> None:
    # 10 days, train 4, test 2: the test windows tile days 4-10 back to back, and each
    # train window is the 4 days just before its test window.
    assert windows(T0, T0 + 10 * DAY, 4 * DAY, 2 * DAY, "1h") == [
        Window(train_start=T0, test_start=T0 + 4 * DAY, test_end=T0 + 6 * DAY),
        Window(train_start=T0 + 2 * DAY, test_start=T0 + 6 * DAY, test_end=T0 + 8 * DAY),
        Window(train_start=T0 + 4 * DAY, test_start=T0 + 8 * DAY, test_end=T0 + 10 * DAY),
    ]


def test_windows_end_at_the_end_and_leave_any_leftover_at_the_start() -> None:
    # One day too many for a fourth window: it's dropped from the start, so the most
    # recent candles are always tested.
    found = windows(T0, T0 + 11 * DAY, 4 * DAY, 2 * DAY, "1h")

    assert [w.train_start for w in found] == [T0 + 1 * DAY, T0 + 3 * DAY, T0 + 5 * DAY]
    assert found[-1].test_end == T0 + 11 * DAY


def test_a_range_too_short_for_one_window_is_refused() -> None:
    with pytest.raises(BacktestError, match="needs at least 6 days"):
        windows(T0, T0 + 5 * DAY, 4 * DAY, 2 * DAY, "1h")


@pytest.mark.parametrize(
    ("train", "test"), [(90 * timedelta(minutes=1), 2 * DAY), (4 * DAY, timedelta(0))]
)
def test_window_lengths_must_be_whole_candles(train: timedelta, test: timedelta) -> None:
    with pytest.raises(ValueError, match="whole number"):
        windows(T0, T0 + 10 * DAY, train, test, "1h")


# --- Walking forward --------------------------------------------------------------------


def test_each_window_tests_the_train_winner_on_the_window_after() -> None:
    results = walk_forward(GRID.strategies, HISTORY, base(), WINDOWS)

    assert len(results) == 6
    for r in results:
        w = r.window
        assert (r.in_sample.config.start, r.in_sample.config.end) == (w.train_start, w.test_start)
        best = sweep(GRID.strategies, HISTORY, base(start=w.train_start, end=w.test_start))[0]
        assert r.in_sample.config == best.config
        assert r.in_sample.result == best.result
        oos = r.out_of_sample
        assert oos.config.params == r.in_sample.config.params  # unchanged
        assert (oos.config.start, oos.config.end) == (w.test_start, w.test_end)
        curve = oos.result.equity_curve
        assert (curve[0].ts, curve[-1].ts, len(curve)) == (w.test_start, w.test_end - HOUR, 100)
        assert oos.config.initial_cash == D(1000)  # fresh cash, flat, every window


def test_the_choice_never_sees_the_candles_it_is_tested_on() -> None:
    # Rewrite every candle from the first test window on: the first window's choice and
    # its in-sample run must not change at all. Only its test can.
    assert WINDOWS[0].test_start == at(400)
    changed = HISTORY[:400] + [wave(h, shift=37) for h in range(400, 1000)]

    before = walk_forward(GRID.strategies, HISTORY, base(), WINDOWS)[0]
    after = walk_forward(GRID.strategies, changed, base(), WINDOWS)[0]

    assert after.in_sample.config == before.in_sample.config
    assert after.in_sample.result == before.in_sample.result
    assert after.out_of_sample.result != before.out_of_sample.result  # the edit did matter


def test_out_of_sample_returns_compound() -> None:
    # 1000 -> +10% -> 1100 -> -5% -> 1045: 4.5% in all.
    assert compounded([D("0.1"), D("-0.05")]) == D("0.045")
    assert compounded([]) == 0


def test_mean_sharpe_skips_runs_without_one() -> None:
    results = walk_forward(GRID.strategies, HISTORY, base(), WINDOWS)
    reports = [r.out_of_sample.report for r in results]
    sharpes = [r.sharpe for r in reports if r.sharpe is not None]

    assert mean_sharpe(reports) == sum(sharpes, D(0)) / len(sharpes)
    assert mean_sharpe([]) is None


# --- Output -----------------------------------------------------------------------------


def test_describe_puts_in_sample_and_out_of_sample_side_by_side() -> None:
    results = walk_forward(GRID.strategies, HISTORY, base(), WINDOWS)
    run_ids = [(10 + 2 * i, 11 + 2 * i) for i in range(len(results))]

    text = describe(results, GRID, run_ids)

    lines = text.splitlines()
    assert lines[0].startswith("walk-forward: ema_crossover v1 on mexc BTC/USDT 1h")
    assert "6 windows" in text
    assert "best of 6 param sets by Sharpe" in text
    groups = next(line for line in lines if "in-sample" in line and "out-of-sample" in line)
    assert groups.index("in-sample") < groups.index("out-of-sample")
    header = next(line for line in lines if line.split()[:1] == ["#"])
    assert header.count("return") == 2
    assert header.count("Sharpe") == 2
    rows = [line for line in lines if line.split()[:1] and line.split()[0].isdigit()]
    assert len(rows) == 6
    assert rows[0].rstrip().endswith("10, 11")
    assert params_of(results[0]) in rows[0]
    compounded_return = compounded(r.out_of_sample.report.total_return for r in results)
    assert f"{compounded_return * 100:+.2f}% compounded" in text


def params_of(result: Any) -> str:
    return " ".join(f"{k}={v}" for k, v in result.in_sample.config.params.items())


# --- Stored runs ------------------------------------------------------------------------


@dataclass(frozen=True)
class Stores:
    candles: CandleStore
    runs: BacktestRunStore


@pytest.fixture
async def stores(db_url: URL) -> AsyncIterator[Stores]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE candles, backtest_runs"))
    candles = CandleStore(engine)
    await candles.upsert(Series("mexc", "BTC/USDT", "1h"), HISTORY)
    yield Stores(candles, BacktestRunStore(engine))
    await engine.dispose()


async def test_each_window_stores_two_runs_that_reproduce(stores: Stores) -> None:
    results, run_ids = await run_walk_forward(
        stores.candles, stores.runs, GRID.strategies, base(), WINDOWS
    )

    assert len(run_ids) == len(results) == 6
    assert len({i for pair in run_ids for i in pair}) == 12
    for r, (in_id, out_id) in zip(results, run_ids, strict=True):
        assert await reproduce(stores.candles, stores.runs, in_id) == r.in_sample.result
        assert await reproduce(stores.candles, stores.runs, out_id) == r.out_of_sample.result
