"""RSI mean reversion on hand-built price series, where the signal candles are known."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position
from engine.strategies.rsi_mean_reversion import RsiMeanReversion, RsiMeanReversionParams

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


def replay(strategy: RsiMeanReversion, closes: list[float]) -> list[tuple[int, Action]]:
    """Feed the series one closed candle at a time; (candle index, action) for each non-HOLD."""
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


def test_v_shaped_series_signals_where_worked_out_by_hand() -> None:
    # period=2 (alpha 1/2); warm-up is 6 x 2 + 1 = 13 candles. While the window starts
    # flat, both averages start at 0, so each step below is easy to follow:
    #   i   close  gain    loss     RSI
    #   14   9     0       0.5       0     oversold, still falling: HOLD
    #   15   8     0       0.75      0
    #   16   7     0       0.875     0     the bottom
    #   17   8     0.5     0.4375   53.3   back up through 30: BUY
    #   18   9     0.75    0.21875  77.4   overbought: SELL
    #   19  10     0.875   0.1094   88.9   still overbought, nothing to sell: HOLD
    strategy = RsiMeanReversion(RsiMeanReversionParams(period=2))
    closes = [10.0] * 14 + [9.0, 8.0, 7.0, 8.0, 9.0, 10.0]

    assert replay(strategy, closes) == [(17, "buy"), (18, "sell")]


def test_buys_on_the_turn_not_while_rsi_keeps_falling() -> None:
    # A long slide keeps RSI at 0, deep in oversold, for 30 candles with no BUY. The BUY
    # waits for a bounce big enough to lift RSI back above 30: after the slide the average
    # loss is about 0.89 a candle, so a +6 candle gives RSI 6/14 / (6/14 + 0.83) = 34.
    strategy = RsiMeanReversion(RsiMeanReversionParams())
    slide = [100.0 - i for i in range(1, 31)]
    closes = [100.0] * strategy.warmup_candles + slide + [slide[-1] + 6]

    assert replay(strategy, closes) == [(len(closes) - 1, "buy")]


def test_constant_prices_never_signal() -> None:
    strategy = RsiMeanReversion(RsiMeanReversionParams())

    assert replay(strategy, [100.0] * 200) == []


def test_turn_up_while_holding_is_hold() -> None:
    strategy = RsiMeanReversion(RsiMeanReversionParams(period=2))
    history = candles([10.0] * 14 + [9.0, 8.0, 7.0, 8.0])

    assert strategy.on_candle(history, None).action == "buy"
    signal = strategy.on_candle(history, LONG)
    assert signal.action == "hold"
    assert "already holding" in signal.reason


def test_overbought_while_flat_is_hold() -> None:
    strategy = RsiMeanReversion(RsiMeanReversionParams(period=2))
    history = candles([10.0] * 14 + [11.0])

    assert strategy.on_candle(history, LONG).action == "sell"
    signal = strategy.on_candle(history, None)
    assert signal.action == "hold"
    assert "nothing to sell" in signal.reason


def test_sells_whenever_overbought_not_only_on_the_cross() -> None:
    # A bot restarted mid-rally still holds a position and must still get out: the exit
    # is a state, so every overbought candle is a SELL while holding (D31).
    strategy = RsiMeanReversion(RsiMeanReversionParams(period=2))
    history = candles([10.0] * 14 + [11.0, 12.0, 13.0])

    assert [strategy.on_candle(history[:end], LONG).action for end in (15, 16, 17)] == [
        "sell",
        "sell",
        "sell",
    ]


def test_holds_until_warmed_up() -> None:
    strategy = RsiMeanReversion(RsiMeanReversionParams(period=2))
    assert strategy.warmup_candles == 13
    assert RsiMeanReversion(RsiMeanReversionParams()).warmup_candles == 85
    # The turn that would be a BUY, one candle short of the warm-up.
    history = candles([10.0] * 8 + [9.0, 8.0, 7.0, 8.0])

    signal = strategy.on_candle(history, None)

    assert signal.action == "hold"
    assert signal.reason == "warming up"
    assert signal.ts == history[-1].ts


def test_no_candles_is_an_error() -> None:
    with pytest.raises(ValueError, match="candle"):
        RsiMeanReversion(RsiMeanReversionParams()).on_candle([], None)


def test_signal_depends_only_on_the_warmup_window() -> None:
    # The parity property (D28): full history and just the warm-up window agree.
    strategy = RsiMeanReversion(RsiMeanReversionParams())
    rng = random.Random(0)
    closes = [100.0]
    for _ in range(499):
        closes.append(round(closes[-1] * (1 + rng.uniform(-0.02, 0.02)), 2))
    history = candles(closes)
    window = strategy.warmup_candles

    actions = set()
    for end in range(window, len(history) + 1):
        for position in (None, LONG):
            full = strategy.on_candle(history[:end], position)
            assert full == strategy.on_candle(history[end - window : end], position)
            actions.add(full.action)
    assert actions == {"buy", "sell", "hold"}  # the walk really went both ways


def test_signal_carries_its_candle_and_inputs() -> None:
    strategy = RsiMeanReversion(RsiMeanReversionParams(period=2))
    history = candles([10.0] * 14 + [9.0, 8.0, 7.0, 8.0])

    signal = strategy.on_candle(history, None)

    assert signal.ts == history[-1].ts
    assert signal.indicators == {
        "prev_rsi": 0.0,
        "rsi": pytest.approx(53.3333, abs=1e-4),  # 0.5 / (0.5 + 0.4375)
    }
    assert signal.stop is None
    assert signal.size_hint is None


def test_oversold_must_be_below_overbought() -> None:
    with pytest.raises(ValidationError, match="oversold"):
        RsiMeanReversionParams(oversold=70, overbought=70)


@pytest.mark.parametrize("level", [0, 100])
def test_levels_must_be_inside_the_rsi_range(level: float) -> None:
    with pytest.raises(ValidationError):
        RsiMeanReversionParams(oversold=level, overbought=level)


def test_unknown_params_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RsiMeanReversionParams.model_validate({"period": 14, "oversld": 25})


def test_params_schema_describes_the_form() -> None:
    schema = RsiMeanReversion.params_model.model_json_schema()

    assert set(schema["properties"]) == {"period", "oversold", "overbought"}
    assert schema["properties"]["period"]["default"] == 14
    assert schema["properties"]["oversold"]["default"] == 30
    assert schema["properties"]["overbought"]["default"] == 70
    assert all("description" in field for field in schema["properties"].values())


def test_identity() -> None:
    assert RsiMeanReversion.name == "rsi_mean_reversion"
    assert RsiMeanReversion.version == 1
