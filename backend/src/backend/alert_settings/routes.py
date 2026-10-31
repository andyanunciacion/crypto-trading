"""The Settings page's alert preferences (P7-1, D59): which alerts go to Telegram.

On the `protected` router (app.py), so a session is checked first. The API writes
`alert_settings` itself, as it writes `risk_settings` (D50): the alerts service keeps no
copy and reads them for each batch it sends, so a toggle applies within a second.

- `GET /alert-settings`: every event with its label, description and whether it's on,
  and how the alerts service is doing (`status`: None if it has never run).
- `PUT /alert-settings/{event}` `{"enabled": bool}`: turn one on or off. No TOTP code:
  an alert changes nothing about trading (the owner's choice, D59).
"""

from collections.abc import Callable
from datetime import datetime
from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy import select, update

from backend.alerts.status import AlertsStatus, is_stale, read_status
from backend.auth.deps import CurrentUser
from shared.alerts import EVENTS, AlertEvent
from shared.db.models import AlertSettingRow

log = structlog.get_logger()

router = APIRouter(tags=["settings"])


class AlertSetting(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: AlertEvent
    label: str
    description: str
    enabled: bool
    updated_at: AwareDatetime


class AlertsView(BaseModel):
    model_config = ConfigDict(frozen=True)

    settings: list[AlertSetting]  # in EVENTS' order
    status: AlertsStatus | None
    stale: bool  # the status is over 30 s old: the service isn't running


class SaveAlertSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _now(request: Request) -> datetime:
    return cast(Callable[[], datetime], request.app.state.clock)()


async def _settings(request: Request) -> list[AlertSetting]:
    async with request.app.state.db.connect() as conn:
        rows = {
            row.event: row
            for row in await conn.execute(
                select(AlertSettingRow.event, AlertSettingRow.enabled, AlertSettingRow.updated_at)
            )
        }
    return [
        AlertSetting(
            event=event,
            label=info.label,
            description=info.description,
            enabled=rows[event].enabled,
            updated_at=rows[event].updated_at,
        )
        for event, info in EVENTS.items()
        if event in rows  # migration 0017 seeds every one
    ]


@router.get("/alert-settings")
async def alert_settings(request: Request) -> AlertsView:
    service = await read_status(cast(Redis, request.app.state.redis))
    return AlertsView(
        settings=await _settings(request),
        status=service,
        stale=service is None or is_stale(service, _now(request)),
    )


@router.put("/alert-settings/{event}")
async def save(
    event: AlertEvent, body: SaveAlertSetting, request: Request, user: CurrentUser
) -> AlertSetting:
    now = _now(request)
    async with request.app.state.db.begin() as conn:
        old = await conn.scalar(
            select(AlertSettingRow.enabled).where(AlertSettingRow.event == event).with_for_update()
        )
        if old is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no {event} row")
        if old != body.enabled:
            await conn.execute(
                update(AlertSettingRow)
                .where(AlertSettingRow.event == event)
                .values(enabled=body.enabled, updated_at=now)
            )
    if old != body.enabled:
        log.info(
            "api.alert_settings.changed", user=user.username, alert=event, enabled=body.enabled
        )
    return next(s for s in await _settings(request) if s.event == event)
