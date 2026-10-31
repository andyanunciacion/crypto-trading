"""Exponential backoff: waits double up to a cap, with jitter, and reset after a success."""

import random

from shared.backoff import Backoff


def test_waits_double_within_their_jitter_range() -> None:
    backoff = Backoff(base=1.0, cap=60.0, rng=random.Random(0))

    for step in (1, 2, 4, 8, 16, 32):
        # "Equal jitter": somewhere between half the step and the whole step.
        assert step / 2 <= backoff.next_delay() <= step


def test_waits_stop_growing_at_the_cap() -> None:
    backoff = Backoff(base=1.0, cap=60.0, rng=random.Random(0))

    delays = [backoff.next_delay() for _ in range(2000)]  # far past any float overflow

    assert all(d <= 60.0 for d in delays)
    assert all(d >= 30.0 for d in delays[6:])  # from the 7th wait on, the step is the cap


def test_reset_starts_again_from_the_base() -> None:
    backoff = Backoff(base=1.0, cap=60.0, rng=random.Random(0))
    for _ in range(5):
        backoff.next_delay()

    backoff.reset()

    assert backoff.attempts == 0
    assert 0.5 <= backoff.next_delay() <= 1.0


def test_jitter_spreads_the_waits() -> None:
    # Two clients that fail at the same moment shouldn't retry at the same moment.
    first = Backoff(rng=random.Random(1)).next_delay()
    second = Backoff(rng=random.Random(2)).next_delay()

    assert first != second
