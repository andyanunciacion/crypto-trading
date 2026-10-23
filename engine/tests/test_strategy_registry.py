"""Rebuilding a strategy from its stored name, version and params."""

import pytest
from pydantic import ValidationError

from engine.strategies.bollinger_breakout import BollingerBreakout, BollingerBreakoutParams
from engine.strategies.ema_crossover import EmaCrossover, EmaCrossoverParams
from engine.strategies.registry import STRATEGIES, build
from engine.strategies.rsi_mean_reversion import RsiMeanReversion, RsiMeanReversionParams


def test_builds_a_strategy_from_stored_values() -> None:
    strategy = build("ema_crossover", 1, {"fast": 5, "slow": 13})

    assert isinstance(strategy, EmaCrossover)
    assert strategy.params == EmaCrossoverParams(fast=5, slow=13)


def test_builds_every_starter_strategy() -> None:
    rsi = build("rsi_mean_reversion", 1, {"period": 7, "oversold": 25})
    bollinger = build("bollinger_breakout", 1, {"width": 2.5})

    assert isinstance(rsi, RsiMeanReversion)
    assert rsi.params == RsiMeanReversionParams(period=7, oversold=25)
    assert isinstance(bollinger, BollingerBreakout)
    assert bollinger.params == BollingerBreakoutParams(width=2.5)


def test_names_match_the_classes() -> None:
    assert all(cls.name == name for name, cls in STRATEGIES.items())


def test_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown strategy 'nope'"):
        build("nope", 1, {})


def test_other_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        build("ema_crossover", 2, {})


def test_bad_params_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        build("ema_crossover", 1, {"fast": 5, "slwo": 13})
