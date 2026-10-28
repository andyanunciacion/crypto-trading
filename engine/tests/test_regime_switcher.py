"""Auto-switching's rule (P6-4, D54), on scripted regime readings.

The detector is replaced by one that reads each candle's regime from a script, so each
case says exactly which regimes it sees; the detector's own rules are test_regime.py's.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from engine.exchange.models import Candle
from engine.regime.detector import Regime, RegimeDetector, RegimeParams, RegimeReading
from engine.regime.switcher import AutoSwitcher, same_config
from engine.strategies.base import Strategy
from engine.strategies.registry import build
from shared.regime import AutoSwitchSettings, MappedConfig

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
EMA = build("ema_crossover", 1, {"fast": 9, "slow": 21})
RSI = build("rsi_mean_reversion", 1, {})
FLAT = build("stay_flat", 1, {})
SHORTHAND: dict[str, Regime] = {
    "U": "trending_up",
    "D": "trending_down",
    "R": "ranging",
    "V": "high_volatility",
    "?": "unclear",
}


def config(strategy: Strategy[Any]) -> MappedConfig:
    return MappedConfig(
        strategy=strategy.name,
        version=strategy.version,
        params=strategy.params.model_dump(mode="json"),
    )


def settings(min_hold: int = 3, **mapping: Strategy[Any]) -> AutoSwitchSettings:
    chosen = {"trending_up": EMA, "trending_down": FLAT, "ranging": RSI, "high_volatility": FLAT}
    chosen.update(mapping)
    return AutoSwitchSettings.model_validate(
        {"mapping": {r: config(s) for r, s in chosen.items()}, "min_hold": min_hold}
    )


class Scripted(RegimeDetector):
    """Reads candle i's regime from `script[i]`, after a 2-candle warm-up."""

    def __init__(self, script: str) -> None:
        super().__init__(RegimeParams())
        self.script = script

    @property
    def warmup_candles(self) -> int:
        return 2

    def detect(self, candles: Sequence[Candle]) -> RegimeReading:
        last = candles[-1]
        if len(candles) < self.warmup_candles:
            return RegimeReading(regime="unclear", ts=last.ts, reason="warming up")
        index = (last.ts - T0) // HOUR
        regime = SHORTHAND[self.script[index]]
        return RegimeReading(
            regime=regime, ts=last.ts, reason=f"scripted {regime}", indicators={"adx": 30.0}
        )


def switcher(script: str, min_hold: int = 3, **mapping: Strategy[Any]) -> AutoSwitcher:
    built = AutoSwitcher(settings(min_hold, **mapping))
    built.detector = Scripted(script)
    return built


def candles(n: int, *, skip: int | None = None) -> list[Candle]:
    """`n` hourly candles from T0; with `skip`, candle `skip` is missing."""
    price = Decimal(100)
    return [
        Candle(ts=T0 + i * HOUR, open=price, high=price, low=price, close=price, volume=Decimal(1))
        for i in range(n + (skip is not None))
        if i != skip
    ]


def test_switches_once_the_regime_has_held_min_hold_candles() -> None:
    script = "UURRRR"
    auto = switcher(script)
    checks = [auto.check(candles(n), EMA, HOUR) for n in range(2, len(script) + 1)]

    # Candles 1..5 (candle 0 is warming up): up, then ranging for 1, 2, 3, 4 candles.
    assert [(c.reading.regime, c.held) for c in checks] == [
        ("trending_up", 1),  # the 2-candle warm-up's first reading
        ("ranging", 1),
        ("ranging", 2),
        ("ranging", 3),
        ("ranging", 3),  # counted up to min_hold
    ]
    assert [c.switch is not None for c in checks] == [False, False, False, True, True]
    switch = checks[3].switch
    assert switch is not None
    assert switch.regime == "ranging"
    assert same_config(switch.target, RSI)
    assert switch.reason == (
        "ranging for 3 candles in a row (scripted ranging): switch to rsi_mean_reversion v1 "
        '{"period": 14, "oversold": 30.0, "overbought": 70.0}'
    )


def test_running_the_regimes_config_already_is_no_switch() -> None:
    check = switcher("RRRRR").check(candles(5), RSI, HOUR)

    assert check.confirmed == "ranging"
    assert check.switch is None


def test_unclear_never_confirms_and_breaks_a_run() -> None:
    auto = switcher("RR?RR??")

    for n in range(2, 8):
        check = auto.check(candles(n), EMA, HOUR)
        assert check.confirmed is None, n
        assert check.switch is None, n
    # Unclear for 2 in a row is still nothing to switch to.
    assert auto.check(candles(7), EMA, HOUR).reading.regime == "unclear"


def test_a_different_regime_in_the_run_resets_it() -> None:
    auto = switcher("RRVRRR")

    assert auto.check(candles(5), EMA, HOUR).held == 2
    assert auto.check(candles(6), EMA, HOUR).switch is not None


def test_two_regimes_on_one_config_never_switch_between_them() -> None:
    # A crash and a downtrend both stay flat: going from one to the other changes nothing.
    auto = switcher("DDDVVVV")

    check = auto.check(candles(7), FLAT, HOUR)
    assert check.confirmed == "high_volatility"
    assert check.switch is None


def test_warming_up_is_no_switch() -> None:
    auto = AutoSwitcher(settings(min_hold=1))  # the real detector: 199 candles

    check = auto.check(candles(50), EMA, HOUR)
    assert check.reading.reason == "warming up"
    assert check.switch is None


def test_a_gap_warms_the_readings_up_again() -> None:
    # Candle 3 is missing: the readings after it start over from the candles after it.
    auto = switcher("RRRRRRRR")

    assert auto.check(candles(5, skip=3), EMA, HOUR).switch is None
    assert auto.check(candles(7, skip=3), EMA, HOUR).switch is not None


def test_min_hold_1_switches_on_the_first_reading() -> None:
    check = switcher("UUR", min_hold=1).check(candles(3), EMA, HOUR)

    assert check.held == 1
    assert check.switch is not None
    assert check.switch.reason.startswith("ranging for 1 candle in a row (scripted ranging)")


def test_window_is_the_detectors_plus_min_hold() -> None:
    auto = AutoSwitcher(settings(min_hold=4))

    assert auto.window == 199 + 3


def test_every_regime_must_be_mapped() -> None:
    with pytest.raises(ValidationError, match="no strategy for high_volatility"):
        AutoSwitchSettings.model_validate(
            {
                "mapping": {
                    "trending_up": config(EMA),
                    "trending_down": config(FLAT),
                    "ranging": config(RSI),
                }
            }
        )
    with pytest.raises(ValidationError):  # unclear is never mapped
        AutoSwitchSettings.model_validate(
            {"mapping": {**settings().model_dump()["mapping"], "unclear": config(EMA)}}
        )


def test_a_mapping_that_no_longer_builds_is_refused() -> None:
    mapping = settings().model_dump()["mapping"]
    old = {**mapping, "ranging": {**mapping["ranging"], "version": 99}}
    with pytest.raises(ValueError, match="version"):
        AutoSwitcher(AutoSwitchSettings.model_validate({"mapping": old}))
    unsaved = {**mapping, "ranging": {"strategy": "rsi_mean_reversion", "params": {}}}
    with pytest.raises(ValueError, match="no strategy version"):
        AutoSwitcher(AutoSwitchSettings.model_validate({"mapping": unsaved}))
    bad = {**mapping, "ranging": {**mapping["ranging"], "params": {"period": 1}}}
    with pytest.raises(ValidationError):
        AutoSwitcher(AutoSwitchSettings.model_validate({"mapping": bad}))
