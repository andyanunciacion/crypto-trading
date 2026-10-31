"""The Settings page's alert preferences (backend/alert_settings/, D59), against a real
database: one toggle per event, saved without a TOTP code."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from structlog.testing import capture_logs

from backend.alerts.status import AlertsStatus, write_status
from backend.testing import T0, Api
from shared.alerts import EVENTS
from shared.db.models import AlertSettingRow


@pytest.fixture(autouse=True)
async def defaults(api: Api) -> None:
    async with api.db.begin() as conn:
        for event, info in EVENTS.items():
            await conn.execute(
                update(AlertSettingRow)
                .where(AlertSettingRow.event == event)
                .values(enabled=info.default)
            )
    assert (await api.login()).status_code == 204


async def test_every_event_with_its_label_and_whether_it_s_on(api: Api) -> None:
    body = (await api.client.get("/alert-settings")).json()

    assert [(s["event"], s["enabled"]) for s in body["settings"]] == [
        (event, info.default) for event, info in EVENTS.items()
    ]
    assert body["settings"][0]["label"] == "Trades"
    # The alerts service has never run here.
    assert body["status"] is None and body["stale"] is True


async def test_turning_one_off_saves_it_without_a_code_and_logs_who(api: Api) -> None:
    with capture_logs() as logs:
        response = await api.client.put("/alert-settings/trade", json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    async with api.db.connect() as conn:
        assert (
            await conn.scalar(
                select(AlertSettingRow.enabled).where(AlertSettingRow.event == "trade")
            )
            is False
        )
    changed = [line for line in logs if line["event"] == "api.alert_settings.changed"]
    assert changed == [
        {
            "event": "api.alert_settings.changed",
            "log_level": "info",
            "user": "andy",
            "alert": "trade",
            "enabled": False,
        }
    ]


async def test_saving_what_s_already_stored_changes_and_logs_nothing(api: Api) -> None:
    with capture_logs() as logs:
        response = await api.client.put("/alert-settings/trade", json={"enabled": True})

    assert response.status_code == 200
    assert not [line for line in logs if line["event"] == "api.alert_settings.changed"]


async def test_an_unknown_event_or_a_bad_body_is_a_422_that_doesn_t_echo_it(api: Api) -> None:
    unknown = await api.client.put("/alert-settings/secret-thing", json={"enabled": True})
    bad = await api.client.put("/alert-settings/trade", json={"enabled": "yes please"})
    extra = await api.client.put("/alert-settings/trade", json={"enabled": True, "x": 1})

    assert [r.status_code for r in (unknown, bad, extra)] == [422, 422, 422]
    assert "secret-thing" not in unknown.text and "yes please" not in bad.text


async def test_the_alerts_services_status_and_whether_it_s_fresh(api: Api) -> None:
    status = AlertsStatus(
        ts=T0 - timedelta(seconds=5), configured=True, last_sent_at=T0, last_error=None
    )
    await write_status(api.redis, status)

    fresh = (await api.client.get("/alert-settings")).json()
    api.clock.tick(60)
    stale = (await api.client.get("/alert-settings")).json()

    assert fresh["status"]["configured"] is True and fresh["stale"] is False
    assert stale["stale"] is True
