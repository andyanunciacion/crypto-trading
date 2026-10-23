"""SignalRunner: closed candles in one at a time, the strategy's signal out, logged."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from engine.exchange.models import Candle
from engine.strategies.base import Position
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from engine.strategies.runner import SignalRunner

BTC = "BTC/USDT"
T0 = datetime(2026, 10, 22, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LONG = Position(amount=Decimal(1), avg_entry=Decimal(100))


def candle(i: int, close: float = 100.0) -> Candle:
    price = Decimal(str(close))
    return Candle(
        ts=T0 + i * MINUTE, open=price, high=price, low=price, close=price, volume=Decimal(1)
    )


def ema() -> EmaCrossover:
    return EmaCrossover(EmaCrossoverParams(fast=3, slow=5))  # warm-up 16


def test_signals_on_the_window_like_the_strategy_does() -> None:
    strategy = ema()
    history = [candle(i) for i in range(30)] + [candle(30, 110.0)]
    runner = SignalRunner(strategy, BTC, "1m")
    runner.seed(history[:-1])

    signal = runner.on_closed(history[-1], None)

    assert signal.action == "buy"
    assert signal == strategy.on_candle(history, None)


def test_keeps_only_the_warmup_window() -> None:
    runner = SignalRunner(ema(), BTC, "1m")
    runner.seed([candle(i) for i in range(100)])

    assert len(runner.window) == 16
    assert runner.window[-1].ts == T0 + 99 * MINUTE


def test_warms_up_from_nothing() -> None:
    runner = SignalRunner(ema(), BTC, "1m")

    signals = [runner.on_closed(candle(i), None) for i in range(16)]

    assert {s.reason for s in signals[:-1]} == {"warming up"}
    assert signals[-1].reason != "warming up"


def test_position_is_passed_through() -> None:
    runner = SignalRunner(ema(), BTC, "1m")
    runner.seed([candle(i) for i in range(30)])

    signal = runner.on_closed(candle(30, 90.0), LONG)  # cross down while holding

    assert signal.action == "sell"


def test_a_gap_holds_until_the_window_is_whole_again() -> None:
    # Like the backtester refusing gaps (D25): no signal from a window with a hole in it.
    runner = SignalRunner(ema(), BTC, "1m")
    runner.seed([candle(i) for i in range(30)])

    after_gap = [runner.on_closed(candle(i, 110.0), None) for i in range(32, 32 + 16)]

    assert all(s.action == "hold" and "gap" in s.reason for s in after_gap[:-1])
    assert "gap" not in after_gap[-1].reason  # 16 contiguous candles since the gap


@pytest.mark.parametrize("i", [29, 28], ids=["repeat", "older"])
def test_a_candle_that_isnt_newer_is_refused(i: int) -> None:
    runner = SignalRunner(ema(), BTC, "1m")
    runner.seed([candle(i) for i in range(30)])

    with pytest.raises(ValueError, match="after"):
        runner.on_closed(candle(i), None)


def test_seed_must_be_in_order() -> None:
    runner = SignalRunner(ema(), BTC, "1m")

    with pytest.raises(ValueError, match="after"):
        runner.seed([candle(1), candle(0)])


def test_every_signal_is_logged_with_its_inputs() -> None:
    runner = SignalRunner(ema(), BTC, "1m")
    runner.seed([candle(i) for i in range(30)])

    with capture_logs() as logs:
        runner.on_closed(candle(30), None)
        runner.on_closed(candle(31, 110.0), None)

    hold, buy = logs
    assert hold["event"] == buy["event"] == "strategy.signal"
    assert hold["log_level"] == "debug"
    assert buy["log_level"] == "info"
    assert buy["action"] == "buy"
    assert buy["strategy"] == "ema_crossover"
    assert buy["version"] == 1
    assert buy["params"] == {"fast": 3, "slow": 5}
    assert buy["symbol"] == BTC
    assert buy["timeframe"] == "1m"
    assert buy["ts"] == (T0 + 31 * MINUTE).isoformat()
    assert buy["holding"] is False
    assert buy["indicators"]["fast_ema"] == 105.0
    assert "crossed above" in buy["reason"]
