"""What a new bot can be set up with, for the dashboard's form (D47).

The engine owns the strategies, sizing modes and stops, so it describes them: at startup
it `SET`s `CATALOG_KEY` to a `Catalog` (no expiry, like the heartbeat), and the API
serves it as is. The API never imports the engine.

Each setting is a `FieldSpec`, read from its pydantic model by `field_specs()`: the
type, the default, the bounds from `Field(ge=..., lt=...)`, and the description. The
form is drawn from these, so a new strategy needs no dashboard change. A field of a
type the form can't draw raises, so a test fails before the form would be wrong. The
engine checks everything again when it gets the command; the form only helps.
"""

from decimal import Decimal
from typing import Literal

import annotated_types
from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic_core import PydanticUndefined

CATALOG_KEY = "engine:catalog"

FieldType = Literal["integer", "number", "decimal"]
_TYPES: dict[object, FieldType] = {int: "integer", float: "number", Decimal: "decimal"}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldSpec(_Frozen):
    """One setting. Numbers are strings, so a Decimal arrives as written (D9)."""

    name: str
    # "decimal" is money-like (sizing, stops): sent back as a string, never via a float.
    type: FieldType
    default: str | None  # None: required
    minimum: str | None = None
    exclusive_minimum: bool = False  # minimum itself is refused
    maximum: str | None = None
    exclusive_maximum: bool = False
    description: str | None = None


class Choice(_Frozen):
    """A strategy, a sizing mode or a stop kind, with its settings."""

    name: str  # the strategy's name, the sizing's `mode` or the stop's `kind`
    title: str
    version: int | None = None  # strategies only
    fields: list[FieldSpec]


class Catalog(_Frozen):
    strategies: list[Choice]
    sizing: list[Choice]
    # By slot (stop_loss, take_profit, trailing): the kinds each one takes.
    stops: dict[str, list[Choice]]
    stops_fields: list[FieldSpec]  # shared by every slot, e.g. atr_period
    timeframes: list[str]
    written_at: AwareDatetime


def field_specs(model: type[BaseModel], *, skip: frozenset[str] = frozenset()) -> list[FieldSpec]:
    """`model`'s fields, in order, except those in `skip` (a discriminator, say)."""
    specs: list[FieldSpec] = []
    for name, info in model.model_fields.items():
        if name in skip:
            continue
        kind = _TYPES.get(info.annotation)
        if kind is None:
            raise TypeError(
                f"{model.__name__}.{name} is {info.annotation!r}; the form draws only "
                f"int, float and Decimal"
            )
        bounds: dict[str, str | bool] = {}
        for rule in info.metadata:
            if isinstance(rule, annotated_types.Ge):
                bounds["minimum"] = str(rule.ge)
            elif isinstance(rule, annotated_types.Gt):
                bounds["minimum"] = str(rule.gt)
                bounds["exclusive_minimum"] = True
            elif isinstance(rule, annotated_types.Le):
                bounds["maximum"] = str(rule.le)
            elif isinstance(rule, annotated_types.Lt):
                bounds["maximum"] = str(rule.lt)
                bounds["exclusive_maximum"] = True
        default = None if info.default is PydanticUndefined else str(info.default)
        specs.append(
            FieldSpec.model_validate(
                {
                    "name": name,
                    "type": kind,
                    "default": default,
                    "description": info.description,
                    **bounds,
                }
            )
        )
    return specs
