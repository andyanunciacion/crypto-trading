"""The Settings page's risk limits (D50): read them, and save an edit.

On the `protected` router (app.py), so a session is checked first.

- `GET /risk-settings`: both modes' limits and circuit breakers (store.py).
- `PUT /risk-settings/{mode}`: replace a mode's settings. Every one must be given (null
  turns it off), so a missing field can't quietly turn a limit off. If the edit loosens
  any of them (shared/risk.py: a higher limit, a shorter window, one turned off), it
  needs a TOTP `code` as well (UX4): without one, or with a wrong one, it's a 403 with
  what it loosens, and nothing is written. Tightening needs no code, and a code sent
  with one isn't used up. The row is held while the edit is judged and written, so it's
  judged against what's really stored. It applies from the next buy (no restart).
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from starlette.datastructures import State

from backend.auth.confirm import CodeRefused, code_refusal
from backend.auth.crypto import Secrets
from backend.auth.deps import CurrentUser
from backend.risk_settings.store import Mode, StoredRiskSettings, lock, read_all, write
from shared.risk import LOOSER_WHEN, RiskSettings, loosened

log = structlog.get_logger()

router = APIRouter(tags=["settings"])


class SaveRiskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: RiskSettings
    code: str | None = Field(default=None, max_length=16)  # TOTP; needed to loosen

    @model_validator(mode="before")
    @classmethod
    def _every_setting_given(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("settings"), dict):
            missing = ", ".join(field for field in LOOSER_WHEN if field not in data["settings"])
            if missing:
                raise ValueError(f"every setting must be given (null turns one off): {missing}")
        return data


def _now(state: State) -> datetime:
    return cast(Callable[[], datetime], state.clock)()


def _show(value: int | Decimal | None) -> JsonValue:
    # Decimals in plain digits: str() can give "1E+1".
    return None if value is None else value if isinstance(value, int) else format(value, "f")


@router.get("/risk-settings")
async def risk_settings(request: Request) -> list[StoredRiskSettings]:
    return await read_all(request.app.state.db)


@router.put("/risk-settings/{mode}")
async def save(
    mode: Mode, body: SaveRiskSettings, request: Request, user: CurrentUser
) -> StoredRiskSettings:
    state = request.app.state
    now = _now(state)
    refused: CodeRefused | None = None
    async with state.db.begin() as conn:
        stored = await lock(conn, mode)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no {mode} row in risk_settings")
        looser = loosened(stored.settings, body.settings)
        if looser:
            refused = await code_refusal(
                conn,
                cast(Secrets, state.secrets),
                user.id,
                body.code,
                now,
                needed_for="loosening a risk limit",
            )
        changes = _changes(stored.settings, body.settings)
        saved = stored
        if refused is None and changes:
            saved = await write(conn, mode, body.settings, now)
    # Out of the transaction, so a wrong code's failure stays counted.
    fields = {"user": user.username, "mode": mode, "changes": changes}
    loosened_json: list[JsonValue] = [
        {"field": c.field, "old": _show(c.old), "new": _show(c.new)} for c in looser
    ]
    if refused is not None:
        log.warning(
            "api.risk_settings.refused", why=refused.detail, loosened=loosened_json, **fields
        )
        refused.extra["loosened"] = loosened_json
        raise refused
    if not changes:
        return saved  # nothing to write, and nothing logged
    log.info(
        "api.risk_settings.changed",
        loosened=[c.field for c in looser],
        with_code=bool(looser),
        **fields,
    )
    return saved


def _changes(old: RiskSettings, new: RiskSettings) -> dict[str, list[JsonValue]]:
    """Each setting the edit changes, as [old, new]."""
    return {
        field: [_show(getattr(old, field)), _show(getattr(new, field))]
        for field in LOOSER_WHEN
        if getattr(old, field) != getattr(new, field)
    }
