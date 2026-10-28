"""Which edits of the risk settings loosen them, and so need a TOTP code (UX4, D50)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from shared.risk import LOOSER_WHEN, Change, RiskSettings, loosened

# The seeded values (ARCHITECTURE §6; migrations 0008 and 0009).
SEEDED = RiskSettings(
    max_open_positions=3,
    max_coin_exposure_pct=Decimal("25"),
    max_daily_loss_pct=Decimal("3"),
    max_drawdown_pct=Decimal("10"),
    stale_after_candles=2,
    max_failed_orders=3,
    failed_orders_minutes=10,
    spike_pct=Decimal("3"),
    spike_minutes=5,
)

# A bigger number lets more through for these...
HIGHER_IS_LOOSER: list[tuple[str, int | Decimal]] = [
    ("max_open_positions", 4),
    ("max_coin_exposure_pct", Decimal("30")),
    ("max_daily_loss_pct", Decimal("5")),
    ("max_drawdown_pct", Decimal("15")),
    ("stale_after_candles", 3),
    ("max_failed_orders", 5),
    ("spike_pct", Decimal("4")),
]
# ...and a smaller one for these: a shorter window catches fewer failures, and smaller swings.
LOWER_IS_LOOSER: list[tuple[str, int | Decimal]] = [
    ("failed_orders_minutes", 5),
    ("spike_minutes", 2),
]


def edit(**changes: object) -> RiskSettings:
    return SEEDED.model_copy(update=changes)


def test_every_setting_has_a_direction() -> None:
    # A setting added later without one would be edited with no code asked.
    assert set(LOOSER_WHEN) == set(RiskSettings.model_fields)


def test_nothing_changed_loosens_nothing() -> None:
    assert loosened(SEEDED, SEEDED) == []


@pytest.mark.parametrize(("field", "looser"), HIGHER_IS_LOOSER + LOWER_IS_LOOSER)
def test_a_looser_value_loosens(field: str, looser: int | Decimal) -> None:
    old = getattr(SEEDED, field)
    assert loosened(SEEDED, edit(**{field: looser})) == [Change(field=field, old=old, new=looser)]


@pytest.mark.parametrize(("field", "looser"), HIGHER_IS_LOOSER + LOWER_IS_LOOSER)
def test_the_other_way_tightens(field: str, looser: int | Decimal) -> None:
    # From the looser value back to the seeded one.
    assert loosened(edit(**{field: looser}), SEEDED) == []


@pytest.mark.parametrize("field", [f for f, _ in HIGHER_IS_LOOSER + LOWER_IS_LOOSER])
def test_turning_one_off_loosens_it_and_turning_it_on_tightens_it(field: str) -> None:
    off = {field: None}
    # The breakers' pairs are off together.
    for pair in (("max_failed_orders", "failed_orders_minutes"), ("spike_pct", "spike_minutes")):
        if field in pair:
            off = dict.fromkeys(pair)
    turned_off = RiskSettings.model_validate({**SEEDED.model_dump(), **off})
    assert {c.field for c in loosened(SEEDED, turned_off)} == set(off)
    assert all(c.new is None for c in loosened(SEEDED, turned_off))
    assert loosened(turned_off, SEEDED) == []


def test_the_same_number_written_differently_is_no_change() -> None:
    assert loosened(SEEDED, edit(max_drawdown_pct=Decimal("10.00"))) == []


def test_each_setting_is_judged_on_its_own() -> None:
    # 3 failures in 10 min → 5 in 30: more failures allowed, over a longer window. One of
    # them is looser, so a code is needed, whatever the pair adds up to.
    mixed = edit(max_failed_orders=5, failed_orders_minutes=30)
    assert loosened(SEEDED, mixed) == [Change(field="max_failed_orders", old=3, new=5)]


def test_several_looser_settings_are_all_listed_in_order() -> None:
    edited = edit(max_open_positions=1, max_drawdown_pct=Decimal("20"), spike_minutes=1)
    assert loosened(SEEDED, edited) == [
        Change(field="max_drawdown_pct", old=Decimal("10"), new=Decimal("20")),
        Change(field="spike_minutes", old=5, new=1),
    ]


def test_settings_keep_their_bounds_and_pairs() -> None:
    for bad in (
        {"max_open_positions": 0},
        {"max_drawdown_pct": Decimal("101")},
        {"spike_minutes": 61},
        {"max_failed_orders": 3},  # without its minutes
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            RiskSettings.model_validate(bad)
