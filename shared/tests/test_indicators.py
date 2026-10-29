"""Indicator math, checked against values worked out by hand."""

import math

import pytest

from shared.indicators import (
    Bands,
    Directional,
    adx,
    atr,
    bollinger,
    efficiency_ratio,
    ema,
    rsi,
)


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


def test_atr_matches_a_hand_calculation() -> None:
    # (high, low, close). True ranges from the second candle on:
    #   max(11 - 9, |11 - 9|,  |9 - 9|)     = 2
    #   max(12 - 9, |12 - 10|, |9 - 10|)    = 3
    #   max(11 - 10, |11 - 11|, |10 - 11|)  = 1
    #   max(15 - 12, |15 - 10.5|, ...)      = 4.5  a gap up: the high minus the last close
    #   max(10 - 8, ..., |8 - 14|)          = 6    a gap down: the last close minus the low
    # Period 3: seed (2 + 3 + 1) / 3 = 2; then 2 + (4.5 - 2) / 3 = 17/6;
    # then 17/6 + (6 - 17/6) / 3 = 35/9.
    candles = [(10, 8, 9), (11, 9, 10), (12, 9, 11), (11, 10, 10.5), (15, 12, 14), (10, 8, 9)]
    highs, lows, closes = ([float(c[i]) for c in candles] for i in range(3))

    assert atr(highs, lows, closes, 3) == pytest.approx([2.0, 17 / 6, 35 / 9])


def test_atr_of_a_flat_market_is_zero() -> None:
    assert atr([5.0] * 4, [5.0] * 4, [5.0] * 4, 2) == [0.0, 0.0]


def test_atr_has_one_value_per_full_period_of_true_ranges() -> None:
    flat = [1.0] * 10
    assert len(atr(flat, flat, flat, 1)) == 9
    assert len(atr(flat, flat, flat, 9)) == 1
    assert atr(flat[:4], flat[:4], flat[:4], 4) == []


def test_atr_needs_equal_series_and_a_positive_period() -> None:
    with pytest.raises(ValueError, match="period"):
        atr([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], 0)
    with pytest.raises(ValueError, match="same length"):
        atr([1.0, 2.0], [1.0], [1.0, 2.0], 1)


def test_adx_matches_a_hand_calculation() -> None:
    # (high, low, close). From the second candle on, up = high - previous high and
    # down = previous low - low; the bigger one counts as +DM or -DM if positive:
    #   up  2, down -1: +DM 2, -DM 0, TR 3
    #   up  1, down -1: +DM 1, -DM 0, TR 3
    #   up -1, down  2: +DM 0, -DM 2, TR 4
    #   up -1, down  1: +DM 0, -DM 1, TR 4
    # Period 2, Wilder's smoothing as in ATR:
    #   seed:   TR 3,    +DM 1.5,   -DM 0  -> +DI 50,     -DI 0,     DX 100
    #   then:   TR 3.5,  +DM 0.75,  -DM 1  -> +DI 150/7,  -DI 200/7, DX 100/7
    #   then:   TR 3.75, +DM 0.375, -DM 1  -> +DI 10,     -DI 80/3,  DX 500/11
    # ADX seed: (100 + 100/7) / 2 = 400/7; then 400/7 + (500/11 - 400/7) / 2 = 3950/77.
    candles = [(10, 8, 9), (12, 9, 11), (13, 10, 12), (12, 8, 9), (11, 7, 8)]
    highs, lows, closes = ([float(c[i]) for c in candles] for i in range(3))

    result = adx(highs, lows, closes, 2)

    assert [tuple(r) for r in result] == [
        pytest.approx((400 / 7, 150 / 7, 200 / 7)),
        pytest.approx((3950 / 77, 10.0, 80 / 3)),
    ]
    assert isinstance(result[0], Directional)


def test_adx_of_a_steady_rise_is_all_one_way() -> None:
    # Every candle a higher high and a higher low: no -DM at all, so every DX is 100.
    highs = [float(i + 1) for i in range(10)]
    lows = [float(i) for i in range(10)]
    closes = [i + 0.5 for i in range(10)]

    for point in adx(highs, lows, closes, 3):
        assert point.adx == pytest.approx(100.0)
        assert point.plus_di > 0
        assert point.minus_di == 0.0


def test_adx_of_a_flat_market_is_zero() -> None:
    assert adx([5.0] * 5, [5.0] * 5, [5.0] * 5, 2) == [Directional(0.0, 0.0, 0.0)] * 2


def test_adx_has_one_value_per_full_period_of_dx() -> None:
    # DX starts at candle `period`, and ADX averages `period` of them.
    flat = [1.0] * 10
    assert len(adx(flat, flat, flat, 1)) == 9
    assert len(adx(flat, flat, flat, 5)) == 1
    assert adx(flat[:9], flat[:9], flat[:9], 5) == []


def test_adx_needs_equal_series_and_a_positive_period() -> None:
    with pytest.raises(ValueError, match="period"):
        adx([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], 0)
    with pytest.raises(ValueError, match="same length"):
        adx([1.0, 2.0], [1.0], [1.0, 2.0], 1)


def test_efficiency_ratio_matches_a_hand_calculation() -> None:
    # Period 3. Changes: +2, -1, +3, +1.
    # At 15 (index 3): net |15 - 11| = 4, path 2 + 1 + 3 = 6 -> 2/3.
    # At 16 (index 4): net |16 - 13| = 3, path 1 + 3 + 1 = 5 -> 3/5.
    assert efficiency_ratio([11.0, 13.0, 12.0, 15.0, 16.0], 3) == [
        pytest.approx(2 / 3),
        pytest.approx(3 / 5),
    ]


def test_efficiency_ratio_of_a_straight_line_is_one_and_of_a_zigzag_zero() -> None:
    assert efficiency_ratio([float(i) for i in range(8)], 4) == [pytest.approx(1.0)] * 4
    assert efficiency_ratio([float(i) for i in range(8, 0, -1)], 4) == [pytest.approx(1.0)] * 4
    assert efficiency_ratio([1.0, 2.0] * 4, 2) == [0.0] * 6


def test_efficiency_ratio_of_a_flat_market_is_zero() -> None:
    assert efficiency_ratio([5.0] * 4, 2) == [0.0, 0.0]


def test_efficiency_ratio_has_one_value_per_full_window_and_a_positive_period() -> None:
    assert len(efficiency_ratio([1.0] * 10, 3)) == 7
    assert efficiency_ratio([1.0] * 3, 3) == []
    with pytest.raises(ValueError, match="period"):
        efficiency_ratio([1.0, 2.0], 0)
