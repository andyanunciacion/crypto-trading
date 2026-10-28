"""The `risk_settings` rows, as the Settings page reads and writes them (D50).

The API writes them itself rather than asking the engine: the engine keeps no copy (it
reads its mode's row fresh on every buy, engine/risk/account.py), so a saved edit
applies from the next buy, and a limit can be lowered with the engine down.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import Row, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from shared.db.models import RiskSettingsRow
from shared.risk import LOOSER_WHEN, RiskSettings

Mode = Literal["paper", "live"]

# Every setting's column, in the model's order.
_COLUMNS = [getattr(RiskSettingsRow, field) for field in LOOSER_WHEN]


class StoredRiskSettings(BaseModel):
    """One mode's row. Decimals go out as strings (D9)."""

    mode: Mode
    settings: RiskSettings
    updated_at: datetime


def _stored(row: Row[Any]) -> StoredRiskSettings:
    values = row._asdict()
    return StoredRiskSettings(
        mode=values.pop("mode"),
        updated_at=values.pop("updated_at"),
        settings=RiskSettings.model_validate(values),
    )


async def read_all(db: AsyncEngine) -> list[StoredRiskSettings]:
    query = select(RiskSettingsRow.mode, RiskSettingsRow.updated_at, *_COLUMNS).order_by(
        RiskSettingsRow.mode.desc()  # paper first
    )
    async with db.connect() as conn:
        return [_stored(row) for row in await conn.execute(query)]


async def lock(conn: AsyncConnection, mode: Mode) -> StoredRiskSettings | None:
    """The mode's row, held (`FOR UPDATE`) until the transaction ends, so the edit is
    judged against what's really stored."""
    query = (
        select(RiskSettingsRow.mode, RiskSettingsRow.updated_at, *_COLUMNS)
        .where(RiskSettingsRow.mode == mode)
        .with_for_update()
    )
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else _stored(row)


async def write(
    conn: AsyncConnection, mode: Mode, settings: RiskSettings, now: datetime
) -> StoredRiskSettings:
    await conn.execute(
        update(RiskSettingsRow)
        .where(RiskSettingsRow.mode == mode)
        .values(**settings.model_dump(), updated_at=now)
    )
    return StoredRiskSettings(mode=mode, settings=settings, updated_at=now)
