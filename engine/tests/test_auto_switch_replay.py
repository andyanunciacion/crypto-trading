"""Auto-switching replayed over BTC/USDT 1d, 2019-2023: P6-4's done-when.

The fixture is test_regime_history.py's (Binance's daily candles; MEXC keeps a year).
A bot that starts on EMA crossover is fed every candle, as the bot runner feeds it, and
takes each switch the rule calls for. Every switch must come only after its regime has
read `min_hold` candles in a row (checked against the detector's own labels, read
independently), carry a reason, and be at least `min_hold` candles after the last one.
"""

import gzip
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.exchange.models import Candle
from engine.regime.detector import Regime, RegimeDetector
from engine.regime.switcher import AutoSwitch, AutoSwitcher
from engine.strategies.base import Strategy
from engine.strategies.registry import build
from shared.regime import AutoSwitchSettings

FIXTURE = Path(__file__).parent / "fixtures" / "binance" / "btcusdt_1d_2019_2023.json.gz"
DAY = timedelta(days=1)
EMA = build("ema_crossover", 1, {"fast": 9, "slow": 21})


def settings(min_hold: int) -> AutoSwitchSettings:
    def mapped(name: str) -> dict[str, Any]:
        return {"strategy": name, "version": 1, "params": {}}

    return AutoSwitchSettings.model_validate(
        {
            "mapping": {
                "trending_up": {**mapped("ema_crossover"), "params": {"fast": 9, "slow": 21}},
                "ranging": mapped("rsi_mean_reversion"),
                "trending_down": mapped("stay_flat"),
                "high_volatility": mapped("stay_flat"),
            },
            "min_hold": min_hold,
        }
    )


@pytest.fixture(scope="module")
def candles() -> list[Candle]:
    rows = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    return [Candle.model_validate(row) for row in rows]


@pytest.fixture(scope="module")
def labels(candles: list[Candle]) -> list[Regime]:
    detector = RegimeDetector(settings(1).detector)
    window = detector.warmup_candles
    return [
        detector.detect(candles[max(0, i + 1 - window) : i + 1]).regime for i in range(len(candles))
    ]


def replay(candles: list[Candle], min_hold: int) -> list[tuple[int, AutoSwitch]]:
    """Every switch a bot starting on EMA crossover makes, by candle index."""
    auto = AutoSwitcher(settings(min_hold))
    current: Strategy[Any] = EMA
    switches = []
    for i in range(len(candles)):
        check = auto.check(candles[max(0, i + 1 - auto.window) : i + 1], current, DAY)
        if check.switch is not None:
            switches.append((i, check.switch))
            current = check.switch.target
    return switches


@pytest.mark.parametrize("min_hold", [3, 5])
def test_switches_only_after_the_minimum_hold(
    candles: list[Candle], labels: list[Regime], min_hold: int
) -> None:
    switches = replay(candles, min_hold)
    for i, switch in switches:
        print(f"{candles[i].ts:%Y-%m-%d} {switch.reason}")

    assert switches, "five years of BTC should switch at least once"
    last = None
    for i, switch in switches:
        held = labels[i + 1 - min_hold : i + 1]
        assert held == [switch.regime] * min_hold, f"{candles[i].ts:%Y-%m-%d}: {held}"
        assert switch.reason.startswith(f"{switch.regime} for {min_hold} candles in a row (")
        if last is not None:
            assert i - last >= min_hold
        last = i


def test_a_longer_hold_switches_less(candles: list[Candle]) -> None:
    counts = {hold: len(replay(candles, hold)) for hold in (1, 3, 5)}
    print(counts)

    assert counts[1] >= counts[3] >= counts[5]
