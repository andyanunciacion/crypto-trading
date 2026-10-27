"""What a new bot can be set up with, for the Bots page's form (shared/catalog.py, D47).

Built from the code itself: every registered strategy's params model, every sizing
mode and every stop kind, so the form can't offer something the engine would refuse
as unknown. The engine writes it to Redis at startup (`publish`).
"""

import sys
from datetime import datetime
from typing import Any, get_args

import structlog
from pydantic import BaseModel
from redis.asyncio import Redis

from engine.exchange.models import Timeframe
from engine.risk.sizing import Sizing
from engine.risk.stops import StopDistance, Stops, TakeProfitDistance
from engine.strategies.base import Strategy
from engine.strategies.registry import STRATEGIES
from shared.catalog import CATALOG_KEY, Catalog, Choice, field_specs

log = structlog.get_logger()

SIZING_TITLES = {
    "all_in": "All in",
    "fixed": "Fixed amount",
    "percent_of_equity": "Percent of equity",
    "atr_risk": "ATR risk",
}
STOP_TITLES = {"percent": "Percent", "atr": "ATR multiple", "r": "R multiple"}


def build_catalog(now: datetime) -> Catalog:
    return Catalog(
        strategies=[_strategy(cls) for cls in STRATEGIES.values()],
        sizing=[_variant(model, "mode", SIZING_TITLES) for model in _variants(Sizing)],
        stops={
            "stop_loss": [_variant(m, "kind", STOP_TITLES) for m in _variants(StopDistance)],
            "take_profit": [
                _variant(m, "kind", STOP_TITLES) for m in _variants(TakeProfitDistance)
            ],
            "trailing": [_variant(m, "kind", STOP_TITLES) for m in _variants(StopDistance)],
        },
        stops_fields=field_specs(Stops, skip=frozenset({"stop_loss", "take_profit", "trailing"})),
        timeframes=list(get_args(Timeframe)),
        written_at=now,
    )


async def publish(redis: Redis, now: datetime) -> None:
    """Write the catalog for the API. A Redis error is logged: the engine trades on."""
    catalog = build_catalog(now)
    try:
        await redis.set(CATALOG_KEY, catalog.model_dump_json())
    except Exception as exc:
        log.warning("engine.catalog.redis_failed", error=type(exc).__name__, detail=str(exc))
        return
    log.info("engine.catalog.published", strategies=[s.name for s in catalog.strategies])


def _strategy(cls: type[Strategy[Any]]) -> Choice:
    # The first line of the strategy's module docstring, e.g. "EMA crossover: for trending markets."
    doc = sys.modules[cls.__module__].__doc__ or cls.name
    return Choice(
        name=cls.name,
        title=doc.strip().splitlines()[0],
        version=cls.version,
        fields=field_specs(cls.params_model),
    )


def _variants(union: Any) -> list[type[BaseModel]]:
    """The models of an `Annotated[A | B, Field(discriminator=...)]`."""
    (members, *_) = get_args(union)
    return list(get_args(members))


def _variant(model: type[BaseModel], tag: str, titles: dict[str, str]) -> Choice:
    name = model.model_fields[tag].default
    return Choice(name=name, title=titles[name], fields=field_specs(model, skip=frozenset({tag})))
