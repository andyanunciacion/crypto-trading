"""EMA crossover on hand-built price series, where the crossing candles are known."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position, Strategy, StrategyParams
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
LONG = Position(amount=Decimal(1), avg_entry=Decimal(100))


def candles(closes: list[float]) -> list[Candle]:
    def one(i: int, close: float) -> Candle:
        price = Decimal(str(close))
        return Candle(
            ts=T0 + i * HOUR, open=price, high=price, low=price, close=price, volume=Decimal(1)
        )

    return [one(i, close) for i, close in enumerate(closes)]


def replay[P: StrategyParams](
    strategy: Strategy[P], closes: list[float]
) -> list[tuple[int, Action]]:
    """Feed the series one closed candle at a time, as the backtester and a live bot do.

    Buying opens a position and selling closes it. Returns (candle index, action) for
    every signal that isn't HOLD.
    """
    history = candles(closes)
    position: Position | None = None
    events: list[tuple[int, Action]] = []
    for i, candle in enumerate(history):
        signal = strategy.on_candle(history[: i + 1], position)
        if signal.action == "buy":
            position = Position(amount=Decimal(1), avg_entry=candle.close)
        elif signal.action == "sell":
            position = None
        if signal.action != "hold":
            events.append((i, signal.action))
    return events


@pytest.mark.parametrize(
    "params", [EmaCrossoverParams(fast=3, slow=5), EmaCrossoverParams()], ids=["3-5", "default"]
)
def test_steps_up_and_down_cross_on_the_step_candle(params: EmaCrossoverParams) -> None:
    # Flat through the warm-up, so both EMAs equal the price. On a jump, the fast EMA
    # (larger alpha) moves further toward the new price than the slow one on that same
    # candle, so the cross happens exactly on the step.
    strategy = EmaCrossover(params)
    up = strategy.warmup_candles + 5
    down = up + 3 * params.slow
    closes = [100.0] * up + [110.0] * (down - up) + [90.0] * 3 * params.slow

    assert replay(strategy, closes) == [(up, "buy"), (down, "sell")]


def test_v_shaped_series_crosses_where_worked_out_by_hand() -> None:
    # fast=2 (alpha 2/3), slow=4 (alpha 0.4); warm-up is 3 x 4 + 1 = 13 candles. Twelve
    # candles at 10 put both EMAs at exactly 10. Then, rounded:
    #   i   close   fast     slow
    #   12   9      9.3333   9.6000   cross down, but nothing to sell: HOLD
    #   13   8      8.4444   8.9600
    #   14   7      7.4815   8.1760   the bottom
    #   15   8      7.8272   8.1056
    #   16   9      8.6091   8.4634   cross up: BUY (two candles after the bottom: EMA lag)
    #   17  10      9.5364   9.0780
    #   18  11     10.5121   9.8468
    #   19   9      9.5040   9.5081   cross down: SELL
    strategy = EmaCrossover(EmaCrossoverParams(fast=2, slow=4))
    closes = [10.0] * 12 + [9.0, 8.0, 7.0, 8.0, 9.0, 10.0, 11.0, 9.0]

    assert replay(strategy, closes) == [(16, "buy"), (19, "sell")]


def test_constant_prices_never_signal() -> None:
    strategy = EmaCrossover(EmaCrossoverParams())

    assert replay(strategy, [100.0] * 200) == []


def test_cross_up_while_holding_is_hold() -> None:
    strategy = EmaCrossover(EmaCrossoverParams(fast=3, slow=5))
    history = candles([100.0] * strategy.warmup_candles + [110.0])

    assert strategy.on_candle(history, None).action == "buy"
    signal = strategy.on_candle(history, LONG)
    assert signal.action == "hold"
    assert "already holding" in signal.reason


def test_cross_down_while_flat_is_hold() -> None:
    strategy = EmaCrossover(EmaCrossoverParams(fast=3, slow=5))
    history = candles([100.0] * strategy.warmup_candles + [90.0])

    assert strategy.on_candle(history, LONG).action == "sell"
    signal = strategy.on_candle(history, None)
    assert signal.action == "hold"
    assert "nothing to sell" in signal.reason


def test_holds_until_warmed_up() -> None:
    strategy = EmaCrossover(EmaCrossoverParams(fast=3, slow=5))
    assert strategy.warmup_candles == 16
    # A step that would be a BUY, one candle short of the warm-up.
    history = candles([100.0] * 14 + [110.0])

    signal = strategy.on_candle(history, None)

    assert signal.action == "hold"
    assert signal.reason == "warming up"
    assert signal.ts == history[-1].ts


def test_no_candles_is_an_error() -> None:
    with pytest.raises(ValueError, match="candle"):
        EmaCrossover(EmaCrossoverParams()).on_candle([], None)


def test_signal_depends_only_on_the_warmup_window() -> None:
    # The parity property: a backtest with years of history and a bot that just
    # restarted with only the warm-up window must give the same signal for a candle.
    strategy = EmaCrossover(EmaCrossoverParams())
    rng = random.Random(0)
    closes = [100.0]
    for _ in range(299):
        closes.append(round(closes[-1] * (1 + rng.uniform(-0.02, 0.02)), 2))
    history = candles(closes)
    window = strategy.warmup_candles

    actions = set()
    for end in range(window, len(history) + 1):
        for position in (None, LONG):
            full = strategy.on_candle(history[:end], position)
            assert full == strategy.on_candle(history[end - window : end], position)
            actions.add(full.action)
    assert actions == {"buy", "sell", "hold"}  # the walk really crossed both ways


def test_signal_carries_its_candle_and_inputs() -> None:
    strategy = EmaCrossover(EmaCrossoverParams(fast=3, slow=5))
    history = candles([100.0] * strategy.warmup_candles + [110.0])

    signal = strategy.on_candle(history, None)

    assert signal.ts == history[-1].ts
    assert signal.indicators == {
        "prev_fast_ema": 100.0,
        "prev_slow_ema": 100.0,
        "fast_ema": 105.0,  # alpha 0.5: halfway to 110
        "slow_ema": pytest.approx(103.3333, abs=1e-4),  # alpha 1/3
    }
    assert signal.stop is None
    assert signal.size_hint is None


def test_fast_must_be_shorter_than_slow() -> None:
    with pytest.raises(ValidationError, match="fast"):
        EmaCrossoverParams(fast=21, slow=21)


def test_unknown_params_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        EmaCrossoverParams.model_validate({"fast": 9, "slow": 21, "slwo": 30})


def test_params_schema_describes_the_form() -> None:
    # The dashboard (P5) renders a strategy's settings form from this schema.
    schema = EmaCrossover.params_model.model_json_schema()

    assert set(schema["properties"]) == {"fast", "slow"}
    assert schema["properties"]["fast"]["default"] == 9
    assert schema["properties"]["slow"]["default"] == 21
    assert all("description" in field for field in schema["properties"].values())


def test_identity() -> None:
    assert EmaCrossover.name == "ema_crossover"
    assert EmaCrossover.version == 1
