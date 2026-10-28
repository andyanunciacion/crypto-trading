"""The risk settings, and which edits of them loosen the account's protection (D41, D42, D50).

`RiskSettings` is one row of `risk_settings`: the limits (engine/risk/limits.py) and the
circuit breakers (engine/risk/breakers.py), each off when None. It lives here, not in
the engine, because the API's Settings page validates edits with the very same model.

Raising a limit needs a TOTP code; lowering one doesn't (UX4). `loosened()` says which
fields an edit loosens. Each field is judged on its own, so an edit that loosens one
and tightens another still needs a code: "it adds up to safer" is a judgement the code
is there to make you stop and think about.
"""

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The longest spike window: how much price history the breakers keep (breakers.py).
MAX_SPIKE_MINUTES = 60


class RiskSettings(BaseModel):
    """The limits and circuit breakers, one row of `risk_settings` per mode. None turns
    one off."""

    # Immutable, no unknown fields, no NaN/Infinity (as the engine's other settings).
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_open_positions: int | None = Field(default=None, ge=1)
    max_coin_exposure_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_daily_loss_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_drawdown_pct: Decimal | None = Field(default=None, gt=0, le=100)
    # The circuit breakers (breakers.py, D42).
    stale_after_candles: int | None = Field(default=None, ge=1)
    max_failed_orders: int | None = Field(default=None, ge=1)
    failed_orders_minutes: int | None = Field(default=None, ge=1)
    spike_pct: Decimal | None = Field(default=None, gt=0)
    spike_minutes: int | None = Field(default=None, ge=1, le=MAX_SPIKE_MINUTES)

    @model_validator(mode="after")
    def _set_in_pairs(self) -> Self:
        for count, minutes in (
            ("max_failed_orders", "failed_orders_minutes"),
            ("spike_pct", "spike_minutes"),
        ):
            if (getattr(self, count) is None) != (getattr(self, minutes) is None):
                raise ValueError(f"{count} and {minutes} go together: set both, or neither")
        return self


# Which way each setting lets more through. A window is looser when shorter: 3 failures
# in 5 minutes are rarer than 3 in 10, and a 2-minute swing is smaller than a 5-minute one.
LOOSER_WHEN: dict[str, Literal["higher", "lower"]] = {
    "max_open_positions": "higher",
    "max_coin_exposure_pct": "higher",
    "max_daily_loss_pct": "higher",
    "max_drawdown_pct": "higher",
    "stale_after_candles": "higher",
    "max_failed_orders": "higher",
    "failed_orders_minutes": "lower",
    "spike_pct": "higher",
    "spike_minutes": "lower",
}


class Change(BaseModel):
    """One setting an edit loosens, from `old` to `new` (None: off)."""

    model_config = ConfigDict(frozen=True)

    field: str
    old: int | Decimal | None
    new: int | Decimal | None


def loosened(old: RiskSettings, new: RiskSettings) -> list[Change]:
    """The settings `new` loosens compared with `old`, in the model's order. Off is the
    loosest a setting can be: turning one off loosens it, turning one on tightens it."""
    changes = []
    for field, looser in LOOSER_WHEN.items():
        before, after = getattr(old, field), getattr(new, field)
        if before is None or before == after:  # already off, or the same value
            continue
        if after is None or (after > before if looser == "higher" else after < before):
            changes.append(Change(field=field, old=before, new=after))
    return changes
