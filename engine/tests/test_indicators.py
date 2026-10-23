"""Indicator math, checked against values worked out by hand."""

import math

import pytest

from engine.strategies.indicators import Bands, bollinger, ema, rsi


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


def test_rsi_matches_a_hand_calculation() -> None:
    # Period 3. The changes are +1, +1, -1, +2, -1. The seed averages the first three:
    # gain (1 + 1 + 0) / 3 = 2/3, loss (0 + 0 + 1) / 3 = 1/3. Each later change moves the
    # averages a third of the way toward it (Wilder's smoothing, alpha = 1 / period).
    # RSI = 100 x gain / (gain + loss):
    #   seed  gain 2/3    loss 1/3    RSI 66.67
    #   +2    gain 10/9   loss 2/9    RSI 83.33
    #   -1    gain 20/27  loss 13/27  RSI 60.61
    assert rsi([1.0, 2.0, 3.0, 2.0, 4.0, 3.0], 3) == pytest.approx([200 / 3, 250 / 3, 2000 / 33])


def test_rsi_is_100_with_no_losses_and_0_with_no_gains() -> None:
    assert rsi([1.0, 2.0, 3.0, 4.0, 5.0], 2) == [100.0] * 3
    assert rsi([5.0, 4.0, 3.0, 2.0, 1.0], 2) == [0.0] * 3


def test_rsi_of_a_constant_series_is_neutral() -> None:
    # No gains and no losses: 0 / 0, taken as 50 so a flat market never looks oversold.
    assert rsi([100.0] * 10, 3) == [50.0] * 7


def test_rsi_has_one_value_per_full_period_of_changes() -> None:
    # It works on changes, so the first value belongs to input number period + 1.
    assert len(rsi([1.0] * 10, 1)) == 9
    assert len(rsi([1.0] * 10, 9)) == 1
    assert rsi([1.0] * 4, 4) == []


def test_rsi_period_must_be_positive() -> None:
    with pytest.raises(ValueError, match="period"):
        rsi([1.0, 2.0], 0)


def test_bollinger_matches_a_hand_calculation() -> None:
    # Period 5: the window 1..5 has mean 3 and population variance (4 + 1 + 0 + 1 + 4) / 5
    # = 2, so the bands sit 2 x sqrt(2) either side. The window 2..6 is the same, shifted.
    s = 2 * math.sqrt(2)

    first, second = bollinger([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 5, 2.0)

    assert first == pytest.approx(Bands(lower=3 - s, middle=3.0, upper=3 + s))
    assert second == pytest.approx(Bands(lower=4 - s, middle=4.0, upper=4 + s))


def test_bollinger_of_a_constant_series_has_no_width() -> None:
    assert bollinger([100.0] * 5, 3, 2.0) == [Bands(100.0, 100.0, 100.0)] * 3


def test_bollinger_has_one_value_per_full_period() -> None:
    assert len(bollinger([1.0] * 10, 1, 2.0)) == 10
    assert len(bollinger([1.0] * 10, 10, 2.0)) == 1
    assert bollinger([1.0] * 3, 4, 2.0) == []


def test_bollinger_period_must_be_positive() -> None:
    with pytest.raises(ValueError, match="period"):
        bollinger([1.0, 2.0], 0, 2.0)
