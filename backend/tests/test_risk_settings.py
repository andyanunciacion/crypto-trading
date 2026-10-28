"""The Settings page's risk limits (backend/risk_settings/, D50), against a real database.

Done-when (P5-7): raising a limit without a TOTP code is rejected by the API. Each test
starts from ARCHITECTURE §6's seeded values and signs in at 12:00:05; the sign-in uses
that step's code, so a test moves the clock on 30 s before confirming with a new one.
"""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from backend.auth.confirm import WRONG_CODE
from backend.testing import Api
from shared.db.models import RiskSettingsRow, UserRow
from shared.testing import set_limits

SEEDED: dict[str, Any] = {
    "max_open_positions": 3,
    "max_coin_exposure_pct": Decimal("25"),
    "max_daily_loss_pct": Decimal("3"),
    "max_drawdown_pct": Decimal("10"),
    "stale_after_candles": 2,
    "max_failed_orders": 3,
    "failed_orders_minutes": 10,
    "spike_pct": Decimal("3"),
    "spike_minutes": 5,
}
# As the API sends them: Decimals as strings.
SEEDED_JSON = {k: str(v) if isinstance(v, Decimal) else v for k, v in SEEDED.items()}
LOOSER = [
    ("max_open_positions", 4),
    ("max_coin_exposure_pct", "30"),
    ("max_daily_loss_pct", "5"),
    ("max_drawdown_pct", "15"),
    ("stale_after_candles", 3),
    ("max_failed_orders", 5),
    ("failed_orders_minutes", 5),  # a shorter window
    ("spike_pct", "4"),
    ("spike_minutes", 2),  # a shorter window
]


@pytest.fixture(autouse=True)
async def seeded(api: Api) -> None:
    await set_limits(api.db, "paper", **SEEDED)
    await set_limits(api.db, "live", **SEEDED)


async def signed_in(api: Api) -> None:
    assert (await api.login()).status_code == 204
    api.clock.tick(30)  # the next code is a new one


async def stored(api: Api, mode: str = "paper") -> dict[str, Any]:
    async with api.db.connect() as conn:
        row = (
            await conn.execute(
                select(RiskSettingsRow.__table__).where(RiskSettingsRow.mode == mode)
            )
        ).one()
    return {k: v for k, v in row._asdict().items() if k in SEEDED}


async def put(api: Api, code: str | None = None, mode: str = "paper", **changes: object) -> Any:
    body: dict[str, object] = {"settings": {**SEEDED_JSON, **changes}}
    if code is not None:
        body["code"] = code
    return await api.client.put(f"/risk-settings/{mode}", json=body)


async def failed_logins(api: Api) -> int:
    async with api.db.connect() as conn:
        return int(await conn.scalar(select(UserRow.failed_logins)) or 0)


async def test_both_modes_are_listed_paper_first(api: Api) -> None:
    await signed_in(api)
    response = await api.client.get("/risk-settings")

    assert response.status_code == 200
    assert [(r["mode"], r["settings"]) for r in response.json()] == [
        ("paper", SEEDED_JSON),
        ("live", SEEDED_JSON),
    ]


@pytest.mark.parametrize(("field", "looser"), LOOSER)
async def test_loosening_any_setting_without_a_code_is_refused(
    api: Api, field: str, looser: object
) -> None:
    await signed_in(api)
    with capture_logs() as logs:
        response = await api.client.put(
            "/risk-settings/paper", json={"settings": {**SEEDED_JSON, field: looser}}
        )

    assert response.status_code == 403
    body = response.json()
    assert body["needs_code"] is True
    assert body["detail"] == "loosening a risk limit needs a code from your authenticator app"
    assert body["loosened"] == [{"field": field, "old": SEEDED_JSON[field], "new": looser}]
    assert await stored(api) == SEEDED  # nothing written
    assert [log["event"] for log in logs] == ["api.risk_settings.refused"]


async def test_turning_one_off_is_loosening_it(api: Api) -> None:
    await signed_in(api)
    response = await put(api, max_drawdown_pct=None)

    assert response.status_code == 403
    assert response.json()["loosened"] == [{"field": "max_drawdown_pct", "old": "10", "new": None}]


async def test_a_mixed_edit_needs_a_code(api: Api) -> None:
    # Tighter on one side, looser on the other: a code is still needed.
    await signed_in(api)
    response = await put(api, max_failed_orders=5, failed_orders_minutes=30)

    assert response.status_code == 403
    assert [c["field"] for c in response.json()["loosened"]] == ["max_failed_orders"]


async def test_loosening_with_the_right_code_is_saved(api: Api) -> None:
    await signed_in(api)
    with capture_logs() as logs:
        response = await put(api, api.code(), max_drawdown_pct="15", max_open_positions=2)

    assert response.status_code == 200
    assert response.json()["settings"]["max_drawdown_pct"] == "15"
    assert response.json()["updated_at"] == api.clock.now.isoformat().replace("+00:00", "Z")
    assert await stored(api) == {
        **SEEDED,
        "max_drawdown_pct": Decimal("15"),
        "max_open_positions": 2,
    }
    (changed,) = [log for log in logs if log["event"] == "api.risk_settings.changed"]
    assert changed["changes"] == {"max_open_positions": [3, 2], "max_drawdown_pct": ["10", "15"]}
    assert changed["loosened"] == ["max_drawdown_pct"]
    assert changed["with_code"] is True
    assert api.code() not in str(logs)  # never the code


async def test_a_code_works_once(api: Api) -> None:
    await signed_in(api)
    code = api.code()
    assert (await put(api, code, max_drawdown_pct="15")).status_code == 200

    again = await put(api, code, max_drawdown_pct="20")
    assert (again.status_code, again.json()["detail"]) == (403, WRONG_CODE)
    assert (await stored(api))["max_drawdown_pct"] == Decimal("15")


async def test_the_code_used_to_sign_in_is_refused(api: Api) -> None:
    assert (await api.login()).status_code == 204
    response = await put(api, api.code(), max_drawdown_pct="15")  # the same step

    assert (response.status_code, response.json()["detail"]) == (403, WRONG_CODE)


async def test_a_wrong_code_is_refused_and_counts_toward_the_lockout(api: Api) -> None:
    await signed_in(api)
    wrong = "000000" if api.code() != "000000" else "111111"

    response = await put(api, wrong, max_drawdown_pct="15")

    assert (response.status_code, response.json()["detail"]) == (403, WRONG_CODE)
    assert await stored(api) == SEEDED
    assert await failed_logins(api) == 1  # committed, though the edit wasn't


async def test_five_wrong_codes_lock_the_user(api: Api) -> None:
    await signed_in(api)
    wrong = "000000" if api.code() != "000000" else "111111"
    answers = [(await put(api, wrong, max_drawdown_pct="15")).status_code for _ in range(5)]
    # Locked: even the right code is refused, unchecked.
    locked = await put(api, api.code(), max_drawdown_pct="15")

    assert answers == [403, 403, 403, 403, 429]
    assert locked.status_code == 429
    assert locked.json()["detail"] == "too many wrong codes or sign-ins: try again in 15 min"
    assert locked.headers["retry-after"] == "900"
    assert await stored(api) == SEEDED
    # The lock is the sign-in's too.
    api.client.cookies.clear()
    assert (await api.login()).status_code == 429


async def test_tightening_needs_no_code(api: Api) -> None:
    await signed_in(api)
    with capture_logs() as logs:
        response = await put(api, max_drawdown_pct="8", failed_orders_minutes=20, spike_minutes=10)

    assert response.status_code == 200
    assert (await stored(api))["max_drawdown_pct"] == Decimal("8")
    (changed,) = [log for log in logs if log["event"] == "api.risk_settings.changed"]
    assert (changed["loosened"], changed["with_code"]) == ([], False)


async def test_a_code_sent_with_a_tightening_is_not_used_up(api: Api) -> None:
    await signed_in(api)
    code = api.code()
    assert (await put(api, code, max_drawdown_pct="8")).status_code == 200

    assert (await put(api, code, max_drawdown_pct="15")).status_code == 200


async def test_turning_a_limit_back_on_needs_no_code(api: Api) -> None:
    await set_limits(api.db, "paper", **{**SEEDED, "max_drawdown_pct": None})
    await signed_in(api)

    assert (await put(api, max_drawdown_pct="10")).status_code == 200


async def test_saving_the_same_values_writes_nothing(api: Api) -> None:
    await signed_in(api)
    before = (await api.client.get("/risk-settings")).json()[0]
    with capture_logs() as logs:
        response = await put(api, max_drawdown_pct="10.00")  # the same number

    assert response.status_code == 200
    assert response.json()["updated_at"] == before["updated_at"]
    assert logs == []


async def test_each_mode_is_its_own_row(api: Api) -> None:
    await signed_in(api)
    assert (await put(api, mode="live", max_open_positions=1)).status_code == 200

    assert (await stored(api, "live"))["max_open_positions"] == 1
    assert await stored(api, "paper") == SEEDED


@pytest.mark.parametrize(
    "settings",
    [
        {**SEEDED_JSON, "max_open_positions": 0},
        {**SEEDED_JSON, "max_drawdown_pct": "101"},
        {**SEEDED_JSON, "spike_minutes": 61},
        {**SEEDED_JSON, "max_failed_orders": None},  # half a pair
        {**SEEDED_JSON, "unknown": 1},
        {k: v for k, v in SEEDED_JSON.items() if k != "max_drawdown_pct"},  # left out
        {**SEEDED_JSON, "max_drawdown_pct": "NaN"},
    ],
)
async def test_settings_that_dont_validate_are_refused(api: Api, settings: object) -> None:
    await signed_in(api)
    response = await api.client.put(
        "/risk-settings/paper", json={"settings": settings, "code": api.code()}
    )

    assert response.status_code == 422
    assert await stored(api) == SEEDED
    # A 422 never echoes what was sent (app.py).
    assert all("input" not in problem for problem in response.json()["detail"])


async def test_an_unknown_mode_is_refused(api: Api) -> None:
    await signed_in(api)
    assert (await put(api, mode="backtest")).status_code == 422
