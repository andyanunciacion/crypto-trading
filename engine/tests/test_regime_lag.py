"""The lag/flip measures (P6-5b), on scripted labels."""

from datetime import date, timedelta

import pytest

from engine.regime.lag import Label, confirmed_changes, flips, recognition
from shared.regime import Regime

START = date(2024, 1, 1)


def labels(*regimes: Regime) -> list[Label]:
    return [(START + timedelta(days=i), regime) for i, regime in enumerate(regimes)]


def day(i: int) -> date:
    return START + timedelta(days=i)


def test_recognition_counts_days_to_the_first_reading_and_to_each_hold() -> None:
    series = labels(
        "unclear", "ranging", "unclear", "ranging", "ranging", "ranging", "ranging", "unclear"
    )

    found = recognition(series, "ranging", day(0), day(7), holds=[1, 3, 5])

    assert found.first == 1
    assert found.confirmed == {1: 1, 3: 5, 5: None}
    assert found.share == pytest.approx(5 / 8)


def test_a_run_that_began_before_the_span_confirms_on_its_first_day() -> None:
    series = labels("trending_up", "trending_up", "trending_up", "trending_up")

    found = recognition(series, "trending_up", day(2), day(3), holds=[3])

    assert found.first == 0
    assert found.confirmed == {3: 0}
    assert found.share == 1.0


def test_a_span_never_recognised_has_no_days() -> None:
    found = recognition(labels("ranging", "unclear"), "trending_down", day(0), day(1), holds=[1])

    assert found.first is None
    assert found.confirmed == {1: None}
    assert found.share == 0.0


def test_recognition_needs_the_labels_to_cover_the_span() -> None:
    with pytest.raises(ValueError, match="cover"):
        recognition(labels("ranging", "ranging"), "ranging", day(0), day(5), holds=[1])
    with pytest.raises(ValueError, match="hold"):
        recognition(labels("ranging", "ranging"), "ranging", day(0), day(1), holds=[0])


def test_confirmed_changes_ignore_unclear_and_short_runs() -> None:
    regimes: list[Regime] = [
        "ranging", "ranging",  # confirmed with a hold of 2: the first, not a change
        "trending_up",  # one candle: too short for 2
        "unclear", "unclear",  # never confirms
        "ranging", "ranging",  # the same regime again: no change
        "trending_up", "trending_up",  # a change
        "ranging", "ranging",  # another
    ]  # fmt: skip

    assert confirmed_changes(regimes, 2) == 2
    # With a hold of 1, every new non-unclear label is confirmed at once.
    assert confirmed_changes(regimes, 1) == 4
    assert confirmed_changes(regimes, 3) == 0


def test_flips_per_year_and_shares() -> None:
    # 4 labels with 2 changes: over 4 / 365.25 years.
    series = labels("ranging", "trending_up", "trending_up", "ranging")

    found = flips(series, holds=[1, 2])

    years = 4 / 365.25
    assert found.per_year == pytest.approx(2 / years)
    assert found.confirmed_per_year == {1: pytest.approx(2 / years), 2: 0.0}
    assert found.shares == {"ranging": 0.5, "trending_up": 0.5}


def test_labels_must_be_consecutive_days() -> None:
    gappy: list[Label] = [(day(0), "ranging"), (day(2), "ranging")]
    with pytest.raises(ValueError, match="consecutive"):
        flips(gappy, holds=[1])
    with pytest.raises(ValueError, match="consecutive"):
        recognition(gappy, "ranging", day(0), day(2), holds=[1])
