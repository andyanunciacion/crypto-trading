"""The Settings page's exchange keys (backend/keys/, D50), against a real database.

Done-when (P5-7): keys are stored encrypted, checked in the database, and are never
sent back to the browser (nor logged).
"""

from typing import Any

import pytest
from sqlalchemy import select, text
from structlog.testing import capture_logs

from backend.auth.confirm import WRONG_CODE
from backend.testing import Api
from shared.db.models import ExchangeAccountRow

# Made up, never real keys; gitleaks:allow tells the secret scan so.
KEY = "mx0vglTESTKEY0123456789abcdA1b2"  # gitleaks:allow
SECRET = "f00dfeedSECRET9876543210zyxwvuts"  # gitleaks:allow
NEW_KEY = "mx0vglROTATED0123456789abcdZ9y8"  # gitleaks:allow
NEW_SECRET = "c0ffeeNEWSECRET9876543210zyxwvu"  # gitleaks:allow
CREDENTIALS = (KEY, SECRET, NEW_KEY, NEW_SECRET)


@pytest.fixture(autouse=True)
async def empty(api: Api) -> None:
    async with api.db.begin() as conn:
        await conn.execute(text("TRUNCATE exchange_accounts RESTART IDENTITY"))


async def signed_in(api: Api) -> None:
    assert (await api.login()).status_code == 204
    api.clock.tick(30)  # the next code is a new one


def new_account(api: Api, **changes: object) -> dict[str, object]:
    return {
        "exchange": "mexc",
        "label": "main",
        "api_key": KEY,
        "api_secret": SECRET,
        "code": api.code(),
        **changes,
    }


async def rows(api: Api) -> list[Any]:
    async with api.db.connect() as conn:
        return list(await conn.execute(select(ExchangeAccountRow.__table__)))


def leaks(text_: str) -> list[str]:
    return [c for c in CREDENTIALS if c in text_]


async def test_a_key_is_stored_encrypted_and_never_sent_back(api: Api) -> None:
    await signed_in(api)
    with capture_logs() as logs:
        added = await api.client.post("/exchange-accounts", json=new_account(api))
        listed = await api.client.get("/exchange-accounts")

    assert added.status_code == 201
    view = {
        "id": 1,
        "exchange": "mexc",
        "label": "main",
        "sandbox": False,
        "key_hint": "A1b2",
        "created_at": "2026-10-26T12:00:35Z",
        "updated_at": "2026-10-26T12:00:35Z",
    }
    assert added.json() == view
    assert listed.json() == [view]
    assert leaks(added.text) == leaks(listed.text) == []
    # In the database: not the plaintext, but it decrypts to it with ENCRYPTION_KEY.
    (row,) = await rows(api)
    assert KEY not in row.api_key_enc and SECRET not in row.api_secret_enc
    assert row.api_key_enc.startswith("gAAAA")  # a Fernet token
    assert api.secrets.decrypt(row.api_key_enc) == KEY
    assert api.secrets.decrypt(row.api_secret_enc) == SECRET
    # Logged with its hint; never the key, the secret or the code.
    (logged,) = [log for log in logs if log["event"] == "api.exchange_account.added"]
    assert (logged["label"], logged["key_hint"]) == ("main", "A1b2")
    assert leaks(str(logs)) == []
    assert api.code() not in str(logs)


async def test_adding_a_key_needs_a_code(api: Api) -> None:
    await signed_in(api)
    response = await api.client.post("/exchange-accounts", json=new_account(api, code=None))

    assert response.status_code == 403
    assert response.json() == {
        "detail": "saving exchange keys needs a code from your authenticator app",
        "needs_code": True,
    }
    assert await rows(api) == []


async def test_a_wrong_code_stores_nothing(api: Api) -> None:
    await signed_in(api)
    wrong = "000000" if api.code() != "000000" else "111111"
    response = await api.client.post("/exchange-accounts", json=new_account(api, code=wrong))

    assert (response.status_code, response.json()["detail"]) == (403, WRONG_CODE)
    assert await rows(api) == []
    assert leaks(response.text) == []


async def test_a_label_in_use_is_a_conflict_and_keeps_the_code(api: Api) -> None:
    await signed_in(api)
    assert (await api.client.post("/exchange-accounts", json=new_account(api))).status_code == 201
    api.clock.tick(30)
    code = api.code()

    taken = await api.client.post("/exchange-accounts", json=new_account(api, code=code))
    other = await api.client.post(
        "/exchange-accounts", json=new_account(api, code=code, label="second")
    )

    assert (taken.status_code, taken.json()) == (409, {"detail": "an account called 'main' exists"})
    assert leaks(taken.text) == []
    assert other.status_code == 201  # the code wasn't used up on the conflict


async def test_keys_are_replaced_with_a_code(api: Api) -> None:
    await signed_in(api)
    await api.client.post("/exchange-accounts", json=new_account(api))
    api.clock.tick(30)
    keys = {"api_key": NEW_KEY, "api_secret": NEW_SECRET}

    refused = await api.client.put("/exchange-accounts/1", json=keys)
    replaced = await api.client.put("/exchange-accounts/1", json={**keys, "code": api.code()})

    assert refused.status_code == 403
    assert replaced.status_code == 200
    assert replaced.json()["key_hint"] == "Z9y8"
    assert leaks(replaced.text) == []
    (row,) = await rows(api)
    assert api.secrets.decrypt(row.api_key_enc) == NEW_KEY
    assert api.secrets.decrypt(row.api_secret_enc) == NEW_SECRET
    missing = await api.client.put("/exchange-accounts/9", json={**keys, "code": api.code()})
    assert missing.status_code == 404


async def test_deleting_needs_no_code(api: Api) -> None:
    await signed_in(api)
    await api.client.post("/exchange-accounts", json=new_account(api))

    assert (await api.client.delete("/exchange-accounts/1")).status_code == 204
    assert await rows(api) == []
    assert (await api.client.delete("/exchange-accounts/1")).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"api_secret": SECRET + "x" * 300},  # too long
        {"api_secret": SECRET[:10]},  # too short
        {"api_secret": SECRET + "\n"},  # a copy-paste slip
        {"exchange": "kraken"},
        {"sandbox": True},  # MEXC has none
        {"label": ""},
        {"withdraw": True},  # unknown fields are refused
    ],
)
async def test_a_bad_account_is_refused_without_echoing_it(
    api: Api, changes: dict[str, object]
) -> None:
    await signed_in(api)
    response = await api.client.post("/exchange-accounts", json=new_account(api, **changes))

    assert response.status_code == 422
    assert leaks(response.text) == []
    assert await rows(api) == []


async def test_binance_may_be_its_testnet(api: Api) -> None:
    await signed_in(api)
    response = await api.client.post(
        "/exchange-accounts", json=new_account(api, exchange="binance", sandbox=True)
    )

    assert response.status_code == 201
    assert response.json()["sandbox"] is True
