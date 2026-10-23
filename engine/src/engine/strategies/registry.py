"""Every strategy by name, so a stored run (and later a bot's config) can rebuild its strategy."""

from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue

from engine.strategies.base import Strategy
from engine.strategies.bollinger_breakout import BollingerBreakout
from engine.strategies.ema_crossover import EmaCrossover
from engine.strategies.rsi_mean_reversion import RsiMeanReversion

STRATEGIES: dict[str, type[Strategy[Any]]] = {
    strategy.name: strategy for strategy in (EmaCrossover, RsiMeanReversion, BollingerBreakout)
}


def strategy_class(name: str) -> type[Strategy[Any]]:
    try:
        return STRATEGIES[name]
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"unknown strategy {name!r}; known: {known}") from None


def build(name: str, version: int, params: Mapping[str, JsonValue]) -> Strategy[Any]:
    """The strategy `name` with `params` validated, if its code is still `version`.

    A different version means the logic changed, so it wouldn't give the same signals.
    """
    cls = strategy_class(name)
    if cls.version != version:
        raise ValueError(f"{name} is now version {cls.version}, not version {version}")
    return cls(cls.params_model.model_validate(params))
