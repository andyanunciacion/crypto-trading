"""Signing in and out through the API (backend/auth/), against a real database.

The `api` fixture (backend.testing) has one user, `andy`, and a clock at 12:00:05 UTC,
5 s into a TOTP step. Each test's codes come from that clock.
"""

from datetime import timedelta

from sqlalchemy import select
from structlog.testing import capture_logs

from backend.auth.deps import SESSION_COOKIE
from backend.auth.tokens import issue_session
from backend.testing import PASSWORD, Api
from shared.db.models import UserRow

WRONG = {"detail": "wrong username, password or code"}


async def test_signing_in_sets_a_session_cookie_that_opens_the_api(api: Api) -> None:
    response = await api.login()

    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=43200"):
        assert attribute in cookie
    me = await api.client.get("/auth/me")
    assert (me.status_code, me.json()) == (200, {"username": "andy"})


async def test_any_wrong_part_gets_the_same_answer(api: Api) -> None:
    wrong_code = "000000" if api.code() != "000000" else "111111"
    attempts = [
        await api.login(password="wrong password!"),
        await api.login(code=wrong_code),
        await api.login(username="nobody"),
        await api.login(code="12345"),  # not 6 digits
    ]

    assert [(r.status_code, r.json()) for r in attempts] == [(401, WRONG)] * 4
    assert SESSION_COOKIE not in api.client.cookies


async def test_a_code_works_once(api: Api) -> None:
    code = api.code()
    assert (await api.login(code=code)).status_code == 204
    api.client.cookies.clear()

    # Seen over a shoulder and typed in again, still within its 30 s: refused.
    assert (await api.login(code=code)).status_code == 401
    # The next step's code is fine.
    api.clock.tick(30)
    assert (await api.login()).status_code == 204


async def test_five_wrong_attempts_lock_the_user_for_fifteen_minutes(api: Api) -> None:
    with capture_logs() as logs:
        answers = [(await api.login(password="wrong password!")).status_code for _ in range(5)]
        # Locked: even the right password and code get no answer either way.
        locked = await api.login()

    assert answers == [401, 401, 401, 401, 429]
    assert locked.status_code == 429
    assert locked.json() == {"detail": "too many failed sign-ins: try again in 15 min"}
    assert locked.headers["retry-after"] == "900"
    outcomes = [log["outcome"] for log in logs if log["event"] == "auth.login"]
    assert outcomes == ["wrong"] * 4 + ["locked", "locked"]
    # Neither the password nor the code is ever logged.
    assert not any(PASSWORD in str(log) or "wrong password!" in str(log) for log in logs)

    api.clock.tick(15 * 60)
    assert (await api.login()).status_code == 204
    async with api.db.connect() as conn:
        row = (await conn.execute(select(UserRow.failed_logins, UserRow.locked_until))).one()
    assert (row.failed_logins, row.locked_until) == (0, None)


async def test_after_a_lock_passes_the_count_starts_again(api: Api) -> None:
    for _ in range(5):
        await api.login(password="wrong password!")
    api.clock.tick(15 * 60)

    # One more wrong attempt is the first of a new count, not the sixth.
    assert (await api.login(password="wrong password!")).status_code == 401


async def test_a_right_sign_in_resets_the_count(api: Api) -> None:
    for _ in range(4):
        await api.login(password="wrong password!")
    assert (await api.login()).status_code == 204
    api.client.cookies.clear()
    api.clock.tick(30)

    assert (await api.login(password="wrong password!")).status_code == 401  # 1, not 5


async def test_a_session_ends_when_it_expires(api: Api) -> None:
    await api.login()
    api.clock.tick(12 * 3600 - 1)
    assert (await api.client.get("/auth/me")).status_code == 200

    api.clock.tick(1)
    assert (await api.client.get("/auth/me")).status_code == 401


async def test_a_forged_or_stale_token_is_refused(api: Api) -> None:
    lifetime = timedelta(hours=1)
    other_secret = api.settings.encryption_key  # a real secret, just not the JWT's
    forged = issue_session(api.user_id, 1, other_secret, api.clock.now, lifetime)
    stale = issue_session(api.user_id, 0, api.settings.jwt_secret, api.clock.now, lifetime)
    nobody = issue_session(999, 1, api.settings.jwt_secret, api.clock.now, lifetime)

    for token in (forged, stale, nobody, "garbage"):
        api.client.cookies.set(SESSION_COOKIE, token)
        assert (await api.client.get("/auth/me")).status_code == 401


async def test_signing_out_ends_every_session(api: Api) -> None:
    await api.login()
    token = api.client.cookies[SESSION_COOKIE]
    api.clock.tick(30)
    await api.login()
    other_browser = api.client.cookies[SESSION_COOKIE]

    response = await api.client.post("/auth/logout")

    assert response.status_code == 204
    assert 'session=""' in response.headers["set-cookie"]  # the browser drops it
    for old in (token, other_browser):
        api.client.cookies.set(SESSION_COOKIE, old)
        assert (await api.client.get("/auth/me")).status_code == 401


async def test_a_sign_in_with_unexpected_fields_is_refused(api: Api) -> None:
    response = await api.client.post(
        "/auth/login",
        json={"username": "andy", "password": PASSWORD, "code": api.code(), "admin": True},
    )
    assert response.status_code == 422
