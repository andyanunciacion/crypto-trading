"""`python -m backend.users`: creating the one user and resetting them (D44), against a
real database. Prompts are answered by the test; nothing reads a terminal."""

import argparse
import io
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pyotp
import pytest
from sqlalchemy import URL, Row, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.auth.crypto import Secrets
from backend.auth.passwords import verify_password
from backend.testing import PASSWORD, T0, TOTP_SECRET, Api, api_settings
from backend.users import UsageError, parse_args, run
from shared.db.models import UserRow
from shared.db.session import make_engine


@pytest.fixture
async def db(db_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users RESTART IDENTITY"))
    yield engine
    await engine.dispose()


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(api_settings().encryption_key)


class Answers:
    """Answers the prompts in order, and remembers what was printed."""

    def __init__(self, *answers: str) -> None:
        self._answers: Iterator[str] = iter(answers)
        self.printed: list[str] = []

    def __call__(self, prompt: str) -> str:
        return next(self._answers)

    def out(self, line: str) -> None:
        self.printed.append(line)

    def secret_shown(self) -> str:
        (line,) = [p for p in self.printed if p.startswith("Or enter this secret by hand: ")]
        return line.rsplit(" ", 1)[1]


async def users(db: AsyncEngine) -> list[Row[Any]]:
    async with db.connect() as conn:
        return list((await conn.execute(select(UserRow.__table__))).all())


async def create(
    db: AsyncEngine, secrets: Secrets, *, right_code: bool = True, argv: list[str] | None = None
) -> Answers:
    """`create andy`, typing PASSWORD twice and then the app's code (or a wrong one)."""
    answers = Answers()

    def ask(prompt: str) -> str:
        # The code is asked for after the secret is shown, so it can be worked out then.
        code = pyotp.TOTP(answers.secret_shown()).at(T0)
        return code if right_code else f"{code[:-1]}{(int(code[-1]) + 1) % 10}"

    password = Answers(PASSWORD, PASSWORD)
    await run(
        parse_args(argv or ["create", "andy"]),
        db,
        secrets,
        ask=ask,
        ask_secret=password,
        out=answers.out,
        now=lambda: T0,
    )
    return answers


async def test_create_shows_a_qr_code_and_saves_the_user_once_a_code_confirms_it(
    db: AsyncEngine, secrets: Secrets
) -> None:
    answers = await create(db, secrets)

    secret = answers.secret_shown()
    assert any("otpauth://totp/crypto-trading:andy?" in p for p in answers.printed)
    assert any("█" in p or "▀" in p for p in answers.printed)  # the QR code
    (user,) = await users(db)
    assert user.username == "andy"
    assert verify_password(user.password_hash, PASSWORD)
    # Encrypted at rest, and the confirming code's step is spent.
    assert secret not in user.totp_secret_enc
    assert secrets.decrypt(user.totp_secret_enc) == secret
    assert user.totp_last_step == int(T0.timestamp()) // 30


async def test_a_wrong_code_saves_nothing(db: AsyncEngine, secrets: Secrets) -> None:
    with pytest.raises(UsageError, match="doesn't match"):
        await create(db, secrets, right_code=False)
    assert await users(db) == []


async def test_there_is_only_ever_one_user(db: AsyncEngine, secrets: Secrets) -> None:
    await create(db, secrets)
    with pytest.raises(UsageError, match="exists already"):
        await create(db, secrets, argv=["create", "someone"])
    assert len(await users(db)) == 1


@pytest.mark.parametrize(
    ("first", "second", "why"),
    [("short", "short", "at least 12"), (PASSWORD, PASSWORD + "!", "differ")],
)
async def test_a_bad_password_saves_nothing(
    db: AsyncEngine, secrets: Secrets, first: str, second: str, why: str
) -> None:
    with pytest.raises(UsageError, match=why):
        await run(
            parse_args(["create", "andy"]),
            db,
            secrets,
            ask=Answers("123456"),
            ask_secret=Answers(first, second),
            out=Answers().out,
        )
    assert await users(db) == []


async def test_the_e2e_setup_creates_a_user_without_prompts(
    db: AsyncEngine, secrets: Secrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    args = parse_args(["create", "e2e", "--password-stdin", "--totp-secret", TOTP_SECRET])

    await run(args, db, secrets, out=Answers().out)

    (user,) = await users(db)
    assert secrets.decrypt(user.totp_secret_enc) == TOTP_SECRET
    assert verify_password(user.password_hash, PASSWORD)


async def test_a_byte_order_mark_from_a_powershell_pipe_isnt_part_of_the_password(
    db: AsyncEngine, secrets: Secrets, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `"pw" | docker compose exec -T api ...` in PowerShell with UTF-8 output encoding
    # sends U+FEFF first: kept, it would be an invisible first character nobody can type.
    monkeypatch.setattr("sys.stdin", io.StringIO("﻿" + PASSWORD + "\r\n"))
    args = parse_args(["create", "andy", "--password-stdin", "--totp-secret", TOTP_SECRET])

    await run(args, db, secrets, out=Answers().out)

    (user,) = await users(db)
    assert verify_password(user.password_hash, PASSWORD)


async def test_resets_end_every_session_and_unlock_clears_a_lockout(api: Api) -> None:
    await api.login()
    assert (await api.client.get("/auth/me")).status_code == 200
    args = argparse.Namespace(command="reset-password", username="andy", password_stdin=False)

    new_password = "a brand new password"
    await run(args, api.db, api.secrets, ask_secret=Answers(new_password, new_password))

    assert (await api.client.get("/auth/me")).status_code == 401  # signed out
    api.clock.tick(30)
    assert (await api.login()).status_code == 401  # the old password is gone
    for _ in range(4):
        await api.login(password="wrong password!")
    assert (await api.login(password=new_password)).status_code == 429  # locked

    await run(parse_args(["unlock", "andy"]), api.db, api.secrets, out=Answers().out)

    assert (await api.login(password=new_password)).status_code == 204


async def test_an_unknown_user_is_said_so(db: AsyncEngine, secrets: Secrets) -> None:
    with pytest.raises(UsageError, match="no user named 'ghost'"):
        await run(parse_args(["unlock", "ghost"]), db, secrets, out=Answers().out)
