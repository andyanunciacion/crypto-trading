"""field_specs (shared/catalog.py): a pydantic model's fields as the Bots page's form draws them."""

from decimal import Decimal
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from shared.catalog import FieldSpec, field_specs


class Settings(BaseModel):
    kind: Literal["x"] = "x"
    period: int = Field(default=14, ge=2, description="in candles")
    level: float = Field(default=30, gt=0, lt=100)
    share: Decimal = Field(le=100, description="percent")


def test_each_field_with_its_type_default_bounds_and_description() -> None:
    assert field_specs(Settings, skip=frozenset({"kind"})) == [
        FieldSpec(
            name="period", type="integer", default="14", minimum="2", description="in candles"
        ),
        FieldSpec(
            name="level",
            type="number",
            default="30",
            minimum="0",
            exclusive_minimum=True,
            maximum="100",
            exclusive_maximum=True,
        ),
        FieldSpec(name="share", type="decimal", default=None, maximum="100", description="percent"),
    ]


def test_a_decimal_default_keeps_its_digits() -> None:
    class Money(BaseModel):
        amount: Decimal = Decimal("0.10")

    assert field_specs(Money)[0].default == "0.10"


def test_a_field_the_form_cant_draw_raises() -> None:
    with pytest.raises(TypeError, match=r"Settings\.kind is"):
        field_specs(Settings)

    class Optional(BaseModel):
        period: int | None = None

    with pytest.raises(TypeError, match="the form draws only int, float and Decimal"):
        field_specs(Optional)
