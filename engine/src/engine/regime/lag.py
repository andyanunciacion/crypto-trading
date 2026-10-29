"""How late a regime detector is, and how often it changes its mind (P6-5b).

Two numbers pull against each other: a detector that recognises a new regime sooner
also tends to flip more on noise, and each flip the switcher confirms is a switch that
costs a round trip. So a variant's report has both:

- **Recognition**, per labeled span (`history.SPANS`): days from the span's start to
  the first reading of its label, and to the first candle where the last `hold`
  readings were all that label, which is when the switcher (D54) would confirm it.
- **Flips**: how often the label changes per year, and how often the *confirmed* regime
  does for each hold (a regime confirmed once its last `hold` readings agree and aren't
  `unclear`, as `AutoSwitcher.check()` does it). That's the switches per year before a
  mapping merges regimes onto one config.

Pure functions of a series of daily labels, one per consecutive candle.
"""

import itertools
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from shared.regime import Regime

Label = tuple[date, Regime]
YEAR_DAYS = 365.25


@dataclass(frozen=True)
class Recognition:
    first: int | None  # days from the span's start to the first reading of its label
    confirmed: dict[int, int | None]  # by hold: days until the last `hold` read it
    share: float  # of the span's days that read as its label


@dataclass(frozen=True)
class Flips:
    per_year: float  # label changes
    confirmed_per_year: dict[int, float]  # by hold: changes of the confirmed regime
    shares: dict[Regime, float]  # of all labels


def recognition(
    labels: Sequence[Label], expected: Regime, start: date, end: date, holds: Sequence[int]
) -> Recognition:
    """How soon `labels` recognised the span `start`..`end` (inclusive) as `expected`.
    A run that began before `start` counts: confirmed on the span's first day is 0."""
    _check_days(labels)
    days = [i for i, (day, _) in enumerate(labels) if start <= day <= end]
    if not days or labels[days[0]][0] != start or labels[days[-1]][0] != end:
        raise ValueError(f"the labels don't cover {start}..{end}")
    regimes = [regime for _, regime in labels]
    first = next((i - days[0] for i in days if regimes[i] == expected), None)
    confirmed: dict[int, int | None] = {}
    for hold in holds:
        if hold < 1:
            raise ValueError(f"hold must be at least 1, got {hold}")
        confirmed[hold] = next(
            (
                i - days[0]
                for i in days
                if i + 1 >= hold and all(r == expected for r in regimes[i + 1 - hold : i + 1])
            ),
            None,
        )
    share = sum(regimes[i] == expected for i in days) / len(days)
    return Recognition(first, confirmed, share)


def flips(labels: Sequence[Label], holds: Sequence[int]) -> Flips:
    """How often `labels` change, raw and once confirmed by each hold."""
    _check_days(labels)
    if len(labels) < 2:
        raise ValueError("flips needs at least two labels")
    regimes = [regime for _, regime in labels]
    years = len(labels) / YEAR_DAYS
    changes = sum(a != b for a, b in itertools.pairwise(regimes))
    confirmed: dict[int, float] = {}
    for hold in holds:
        if hold < 1:
            raise ValueError(f"hold must be at least 1, got {hold}")
        confirmed[hold] = confirmed_changes(regimes, hold) / years
    counts = Counter(regimes)
    shares = {regime: count / len(regimes) for regime, count in counts.items()}
    return Flips(changes / years, confirmed, shares)


def confirmed_changes(regimes: Sequence[Regime], hold: int) -> int:
    """How many times the confirmed regime changed: each time `hold` readings in a row
    agree on a regime other than `unclear` and the last one confirmed. The first
    confirmation isn't a change."""
    current: Regime | None = None
    run = 0
    changes = 0
    for i, regime in enumerate(regimes):
        run = run + 1 if i > 0 and regime == regimes[i - 1] else 1
        if regime == "unclear" or run < hold or regime == current:
            continue
        if current is not None:
            changes += 1
        current = regime
    return changes


def _check_days(labels: Sequence[Label]) -> None:
    for (a, _), (b, _) in itertools.pairwise(labels):
        if (b - a).days != 1:
            raise ValueError(f"labels must be for consecutive days: {a} then {b}")
