"""Indicator math, checked against values worked out by hand."""

import pytest

from engine.strategies.indicators import ema


def test_ema_matches_a_hand_calculation() -> None:
    # Period 3, so alpha = 2 / (3 + 1) = 0.5. The seed is the simple average of the first
    # three values, (1 + 2 + 3) / 3 = 2; each later value moves halfway to the next price.
    assert ema([1.0, 2.0, 3.0, 4.0, 6.0, 2.0], 3) == [2.0, 3.0, 4.5, 3.25]


def test_ema_of_a_constant_series_is_that_constant() -> None:
    assert ema([100.0] * 10, 4) == [100.0] * 7


def test_ema_has_one_value_per_full_period() -> None:
    # The first value belongs to the period-th input; one more per input after that.
    assert len(ema([1.0] * 10, 1)) == 10
    assert len(ema([1.0] * 10, 10)) == 1
    assert ema([1.0] * 3, 4) == []


def test_ema_period_must_be_positive() -> None:
    with pytest.raises(ValueError, match="period"):
        ema([1.0, 2.0], 0)
