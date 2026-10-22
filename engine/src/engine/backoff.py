"""Exponential backoff with jitter, for reconnects (P1-3) and network retries (P1-4)."""

import random

# 2**64 seconds is already far past any cap; stopping here keeps the maths in range.
_MAX_DOUBLINGS = 64


class Backoff:
    """Waits of about 1, 2, 4, 8 ... seconds after each failure, up to `cap`.

    Each wait is random, between half and all of the current step ("equal jitter").
    That keeps the growth, but clients that failed together (say, on one network
    outage) don't all retry at the same instant. Call `reset()` after a success.
    """

    def __init__(
        self, base: float = 1.0, cap: float = 60.0, *, rng: random.Random | None = None
    ) -> None:
        self.base = base
        self.cap = cap
        self.attempts = 0  # failures since the last reset
        self._rng = rng or random.Random()

    def next_delay(self) -> float:
        """Seconds to wait before the next try. Each call counts one more failure."""
        step = min(self.cap, self.base * 2 ** min(self.attempts, _MAX_DOUBLINGS))
        self.attempts += 1
        return self._rng.uniform(step / 2, step)

    def reset(self) -> None:
        self.attempts = 0
