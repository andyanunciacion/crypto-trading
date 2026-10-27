"""The catalog the Bots page's form is drawn from (control/catalog.py, D47): every
registered strategy, sizing mode and stop kind, with settings the engine accepts."""

from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from engine.control.catalog import build_catalog, publish
from engine.risk.sizing import SIZING
from engine.risk.stops import STOPS
from engine.strategies.registry import STRATEGIES, build
from shared.catalog import CATALOG_KEY, Catalog, Choice, FieldSpec

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def defaults(choice: Choice) -> dict[str, Any]:
    """Each setting at its default, or, for a required one, at its minimum (or 1)."""
    values: dict[str, Any] = {}
    for spec in choice.fields:
        values[spec.name] = spec.default or str(int(spec.minimum or 0) + 1)
    return values


def test_every_strategy_is_offered_and_its_defaults_build() -> None:
    catalog = build_catalog(NOW)

    assert [s.name for s in catalog.strategies] == list(STRATEGIES)
    for choice in catalog.strategies:
        assert choice.version == STRATEGIES[choice.name].version
        assert choice.title and all(spec.description for spec in choice.fields), choice
        build(choice.name, choice.version, defaults(choice))


def test_the_ema_crossovers_settings() -> None:
    (ema,) = [s for s in build_catalog(NOW).strategies if s.name == "ema_crossover"]

    assert ema.title == "EMA crossover: for trending markets."
    assert ema.fields == [
        FieldSpec(
            name="fast",
            type="integer",
            default="9",
            minimum="2",
            description="Fast EMA period, in candles",
        ),
        FieldSpec(
            name="slow",
            type="integer",
            default="21",
            minimum="3",
            description="Slow EMA period, in candles",
        ),
    ]


def test_every_sizing_mode_and_stop_kind_is_offered_and_parses() -> None:
    catalog = build_catalog(NOW)

    assert [m.name for m in catalog.sizing] == ["all_in", "fixed", "percent_of_equity", "atr_risk"]
    for mode in catalog.sizing:
        SIZING.validate_python({"mode": mode.name, **defaults(mode)})
    assert {slot: [k.name for k in kinds] for slot, kinds in catalog.stops.items()} == {
        "stop_loss": ["percent", "atr"],
        "take_profit": ["percent", "r"],
        "trailing": ["percent", "atr"],
    }
    for slot, kinds in catalog.stops.items():
        for kind in kinds:
            stops = {slot: {"kind": kind.name, **defaults(kind)}}
            if kind.name == "r":
                stops["stop_loss"] = {"kind": "percent", "percent": "3"}
            STOPS.validate_python(stops)
    assert [f.name for f in catalog.stops_fields] == ["atr_period"]
    assert catalog.timeframes[0] == "1m"


async def test_publish_writes_it_for_the_api(redis_url: str) -> None:
    redis = Redis.from_url(redis_url)
    try:
        await publish(redis, NOW)
        raw = await redis.get(CATALOG_KEY)
    finally:
        await redis.aclose()

    assert raw is not None and Catalog.model_validate_json(raw) == build_catalog(NOW)
