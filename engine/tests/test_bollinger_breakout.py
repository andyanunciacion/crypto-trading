"""Bollinger breakout on hand-built price and volume series, where the signal candles are known."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.strategies.base import Action, Position
from engine.strategies.bollinger_breakout import BollingerBreakout, BollingerBreakoutParams

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
LONG = Position(amount=Decimal(1), avg_entry=Decimal(100))

# period 3 and width 1, so the bands are easy to work out by hand. (With width 2, a
# 3-candle band can never be broken: a close is at most sqrt(3 - 1) = 1.41 standard
# deviations from a mean that includes it.)
SMALL = BollingerBreakoutParams(period=3, width=1.0, volume_factor=1.5)


def candles(closes: list[float], volumes: list[float] | None = None) -> list[Candle]:
    """One candle per close; volume 1 unless `volumes` says otherwise."""

    def one(i: int, close: float, volume: float) -> Candle:
        price = Decimal(str(close))
        return Candle(
            ts=T0 + i * HOUR,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal(str(volume)),
        )

    volumes = volumes or [1.0] * len(closes)
    return [
        one(i, close, volume) for i, (close, volume) in enumerate(zip(closes, volumes, strict=True))
    ]


def replay(strategy: BollingerBreakout, history: list[Candle]) -> list[tuple[int, Action]]:
    """Feed the candles one at a time; (candle index, action) for each non-HOLD."""
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


# The breakout worked out by hand; warm-up is 3 + 1 = 4 candles.
#   i   close  vol  middle   upper    note
#   4   10     1    10       10       flat: zero-width bands
#   5   11     2    10.333   10.805   crosses above upper; vol 2 >= 1.5 x avg 1: BUY
#   6   12     1    11       11.816   above the band, but already holding: HOLD
#   7   11.6   1    11.533   11.944   above the middle: HOLD
#   8   11     1    11.533   11.944   below the middle: SELL
BREAKOUT_CLOSES = [10.0] * 5 + [11.0, 12.0, 11.6, 11.0]
BREAKOUT_VOLUMES = [1.0] * 5 + [2.0, 1.0, 1.0, 1.0]


def test_breakout_signals_where_worked_out_by_hand() -> None:
    history = candles(BREAKOUT_CLOSES, BREAKOUT_VOLUMES)

    assert replay(BollingerBreakout(SMALL), history) == [(5, "buy"), (8, "sell")]


def test_breakout_without_volume_is_hold() -> None:
    volumes = [1.0] * 5 + [1.4, 1.0, 1.0, 1.0]  # 1.4 < 1.5 x the average of 1
    history = candles(BREAKOUT_CLOSES, volumes)
    strategy = BollingerBreakout(SMALL)

    assert replay(strategy, history) == []
    signal = strategy.on_candle(history[:6], None)
    assert signal.action == "hold"
    assert signal.reason == "close crossed above the upper band without volume"


def test_volume_factor_zero_ignores_volume() -> None:
    history = candles(BREAKOUT_CLOSES, [1.0] * 5 + [0.0, 1.0, 1.0, 1.0])
    strategy = BollingerBreakout(SMALL.model_copy(update={"volume_factor": 0}))

    assert replay(strategy, history) == [(5, "buy"), (8, "sell")]


def test_breakout_with_default_params() -> None:
    # After flat candles, any jump lands sqrt(20 - 1) = 4.36 standard deviations above
    # the middle of a 20-candle band that includes it, so it clears width 2. The SELL
    # comes when 100 drops below the middle, which the four 110s have lifted to 102.
    strategy = BollingerBreakout(BollingerBreakoutParams())
    closes = [100.0] * 25 + [110.0] + [110.0] * 3 + [100.0]
    volumes = [1.0] * 25 + [2.0] + [1.0] * 4

    assert replay(strategy, candles(closes, volumes)) == [(25, "buy"), (29, "sell")]


def test_constant_prices_never_signal() -> None:
    strategy = BollingerBreakout(BollingerBreakoutParams())

    assert replay(strategy, candles([100.0] * 100)) == []


def test_breakout_while_holding_is_hold() -> None:
    strategy = BollingerBreakout(SMALL)
    history = candles(BREAKOUT_CLOSES[:6], BREAKOUT_VOLUMES[:6])

    assert strategy.on_candle(history, None).action == "buy"
    signal = strategy.on_candle(history, LONG)
    assert signal.action == "hold"
    assert "already holding" in signal.reason


def test_below_the_middle_while_flat_is_hold() -> None:
    strategy = BollingerBreakout(SMALL)
    history = candles([10.0] * 4 + [9.0])

    assert strategy.on_candle(history, LONG).action == "sell"
    signal = strategy.on_candle(history, None)
    assert signal.action == "hold"
    assert "nothing to sell" in signal.reason


def test_sells_whenever_below_the_middle_not_only_on_the_cross() -> None:
    # The exit is a state (D31): a bot that missed the candle that crossed the middle
    # still sells on the next one.
    strategy = BollingerBreakout(SMALL)
    history = candles([10.0] * 4 + [9.0, 8.0, 7.0])

    assert [strategy.on_candle(history[:end], LONG).action for end in (5, 6, 7)] == [
        "sell",
        "sell",
        "sell",
    ]


def test_holds_until_warmed_up() -> None:
    strategy = BollingerBreakout(SMALL)
    assert strategy.warmup_candles == 4
    assert BollingerBreakout(BollingerBreakoutParams()).warmup_candles == 21
    # A breakout that would be a BUY, one candle short of the warm-up.
    history = candles([10.0] * 2 + [11.0], [1.0] * 2 + [2.0])

    signal = strategy.on_candle(history, None)

    assert signal.action == "hold"
    assert signal.reason == "warming up"
    assert signal.ts == history[-1].ts


def test_no_candles_is_an_error() -> None:
    with pytest.raises(ValueError, match="candle"):
        BollingerBreakout(BollingerBreakoutParams()).on_candle([], None)


def test_signal_depends_only_on_the_warmup_window() -> None:
    # The parity property (D28): full history and just the warm-up window agree.
    strategy = BollingerBreakout(BollingerBreakoutParams())
    rng = random.Random(0)
    closes = [100.0]
    for _ in range(499):
        closes.append(round(closes[-1] * (1 + rng.uniform(-0.02, 0.02)), 2))
    volumes = [round(rng.uniform(0.5, 3.0), 3) for _ in closes]
    history = candles(closes, volumes)
    window = strategy.warmup_candles

    actions = set()
    for end in range(window, len(history) + 1):
        for position in (None, LONG):
            full = strategy.on_candle(history[:end], position)
            assert full == strategy.on_candle(history[end - window : end], position)
            actions.add(full.action)
    assert actions == {"buy", "sell", "hold"}  # the walk really broke out and fell back


def test_signal_carries_its_candle_and_inputs() -> None:
    strategy = BollingerBreakout(SMALL)
    history = candles(BREAKOUT_CLOSES[:6], BREAKOUT_VOLUMES[:6])

    signal = strategy.on_candle(history, None)

    assert signal.ts == history[-1].ts
    assert signal.indicators == {
        "prev_close": 10.0,
        "prev_upper": 10.0,
        "close": 11.0,
        "upper": pytest.approx(10.8047, abs=1e-4),  # 31/3 + sqrt(2/9)
        "middle": pytest.approx(10.3333, abs=1e-4),
        "lower": pytest.approx(9.8619, abs=1e-4),
        "volume": 2.0,
        "avg_volume": 1.0,
    }
    assert signal.stop is None
    assert signal.size_hint is None


@pytest.mark.parametrize(
    "params",
    [{"period": 1}, {"width": 0}, {"volume_factor": -1}],
    ids=["period", "width", "volume_factor"],
)
def test_out_of_range_params_are_rejected(params: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        BollingerBreakoutParams.model_validate(params)


def test_unknown_params_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        BollingerBreakoutParams.model_validate({"period": 20, "widht": 2})


def test_params_schema_describes_the_form() -> None:
    schema = BollingerBreakout.params_model.model_json_schema()

    assert set(schema["properties"]) == {"period", "width", "volume_factor"}
    assert schema["properties"]["period"]["default"] == 20
    assert schema["properties"]["width"]["default"] == 2.0
    assert schema["properties"]["volume_factor"]["default"] == 1.5
    assert all("description" in field for field in schema["properties"].values())


def test_identity() -> None:
    assert BollingerBreakout.name == "bollinger_breakout"
    assert BollingerBreakout.version == 1
