"""Create the dashboard's one user, or reset their password or authenticator (D44).

Run inside the stack, interactively (-it), so it can ask for the password:
    docker compose run --rm -it api python -m backend.users create andy
It asks for a password twice, shows a QR code for your authenticator app (and the
secret, to type in by hand), then asks for a code from the app, and saves the user only
if that code is right. Other commands: `reset-password`, `reset-totp` (each ends every
session), and `unlock` (after too many failed sign-ins).

Only someone with a shell on the server can run this. The API itself can't create or
change users.
"""

import argparse
import asyncio
import io
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from getpass import getpass

import segno
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.auth.crypto import Secrets
from backend.auth.passwords import MIN_LENGTH, hash_password
from backend.auth.totp import accepted_step, new_secret, provisioning_uri
from backend.settings import load_api_settings
from shared.db.models import UserRow
from shared.db.session import make_engine

USERNAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
BASE32_SECRET = re.compile(r"[A-Z2-7]{16,}")

Ask = Callable[[str], str]


class UsageError(Exception):
    """Something the person running the command got wrong; shown without a traceback."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- The database --------------------------------------------------------------------------


async def create_user(
    db: AsyncEngine,
    secrets: Secrets,
    username: str,
    password: str,
    totp_secret: str,
    *,
    last_step: int | None,
) -> int:
    """Add the user. There's only ever one: a second is refused. `last_step` is the step
    of the code that confirmed the authenticator, so that code can't sign in too."""
    async with db.begin() as conn:
        if await conn.scalar(select(func.count()).select_from(UserRow)):
            raise UsageError("a user exists already: there's only one (use reset-password)")
        user_id: int = (
            await conn.execute(
                insert(UserRow)
                .values(
                    username=username,
                    password_hash=hash_password(password),
                    totp_secret_enc=secrets.encrypt(totp_secret),
                    totp_last_step=last_step,
                )
                .returning(UserRow.id)
            )
        ).scalar_one()
    return user_id


async def _update(db: AsyncEngine, username: str, **values: object) -> None:
    async with db.begin() as conn:
        result = await conn.execute(
            update(UserRow)
            .where(UserRow.username == username)
            .values(**values, updated_at=_utcnow())
        )
    if result.rowcount != 1:
        raise UsageError(f"no user named {username!r}")


async def reset_password(db: AsyncEngine, username: str, password: str) -> None:
    """New password; every session ends."""
    await _update(
        db,
        username,
        password_hash=hash_password(password),
        session_version=UserRow.session_version + 1,
    )


async def reset_totp(
    db: AsyncEngine, secrets: Secrets, username: str, totp_secret: str, last_step: int
) -> None:
    """New authenticator secret; every session ends."""
    await _update(
        db,
        username,
        totp_secret_enc=secrets.encrypt(totp_secret),
        totp_last_step=last_step,
        session_version=UserRow.session_version + 1,
    )


async def unlock(db: AsyncEngine, username: str) -> None:
    await _update(db, username, failed_logins=0, locked_until=None)


# --- Asking --------------------------------------------------------------------------------


def ask_new_password(ask_secret: Ask) -> str:
    password = ask_secret(f"New password (at least {MIN_LENGTH} characters): ")
    if len(password) < MIN_LENGTH:
        raise UsageError(f"the password must have at least {MIN_LENGTH} characters")
    if ask_secret("The same again: ") != password:
        raise UsageError("the two passwords differ")
    return password


def enrol(
    username: str, ask: Ask, *, out: Callable[[str], None] = print, now: Callable[[], datetime]
) -> tuple[str, int]:
    """Show a new TOTP secret as a QR code and as text, and check a code from the app.

    Returns the secret and the step of the code that confirmed it.
    """
    secret = new_secret()
    uri = provisioning_uri(secret, username)
    drawn = io.StringIO()
    # Compact: half-block characters, two rows of the code per line of text.
    segno.make(uri, error="m").terminal(out=drawn, compact=True)
    out("Scan this with your authenticator app (Aegis, 1Password, Google Authenticator...):")
    out(drawn.getvalue())
    out(f"Or enter this secret by hand: {secret}")
    out(f"({uri})")
    code = ask("Then type the 6-digit code the app shows: ").strip()
    step = accepted_step(secret, code, now(), None)
    if step is None:
        raise UsageError("that code doesn't match, so nothing was saved: run the command again")
    return secret, step


# --- The command ---------------------------------------------------------------------------


async def run(
    args: argparse.Namespace,
    db: AsyncEngine,
    secrets: Secrets,
    *,
    ask: Ask = input,
    ask_secret: Ask = getpass,
    out: Callable[[str], None] = print,
    now: Callable[[], datetime] = _utcnow,
) -> None:
    username: str = args.username
    if not USERNAME.fullmatch(username):
        raise UsageError("a username is 1-64 letters, digits, '_', '.' or '-'")
    password_stdin = getattr(args, "password_stdin", False)
    if args.command == "create":
        password = _password_from_stdin() if password_stdin else ask_new_password(ask_secret)
        step: int | None = None
        if args.totp_secret is not None:
            # For the e2e tests' setup: no person to scan a code.
            if not BASE32_SECRET.fullmatch(args.totp_secret):
                raise UsageError("--totp-secret must be base32 (A-Z, 2-7), 16 characters or more")
            secret = args.totp_secret
        else:
            secret, step = enrol(username, ask, out=out, now=now)
        user_id = await create_user(db, secrets, username, password, secret, last_step=step)
        out(f"Created user {user_id} ({username}). Sign in at the dashboard.")
    elif args.command == "reset-password":
        password = _password_from_stdin() if password_stdin else ask_new_password(ask_secret)
        await reset_password(db, username, password)
        out(f"New password for {username}; every session has ended.")
    elif args.command == "reset-totp":
        secret, step = enrol(username, ask, out=out, now=now)
        await reset_totp(db, secrets, username, secret, step)
        out(f"New authenticator for {username}; every session has ended.")
    elif args.command == "unlock":
        await unlock(db, username)
        out(f"Unlocked {username}.")
    else:  # argparse only allows the four
        raise AssertionError(args.command)


def _password_from_stdin() -> str:
    # PowerShell pipes text with a byte-order mark (U+FEFF) first when its output
    # encoding is UTF-8: an invisible character nobody could type at the sign-in page.
    password = sys.stdin.readline().removeprefix("﻿").rstrip("\r\n")
    if len(password) < MIN_LENGTH:
        raise UsageError(f"the password must have at least {MIN_LENGTH} characters")
    return password


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create the one user")
    create.add_argument("username")
    create.add_argument("--password-stdin", action="store_true", help="read it from stdin")
    create.add_argument(
        "--totp-secret", help="use this base32 secret, without asking for a code (tests only)"
    )
    reset = commands.add_parser("reset-password", help="set a new password")
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true", help="read it from stdin")
    commands.add_parser("reset-totp", help="set up a new authenticator").add_argument("username")
    commands.add_parser("unlock", help="clear a lockout").add_argument("username")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> None:
    settings = load_api_settings()
    db = make_engine(settings.database_url)
    try:
        await run(args, db, Secrets(settings.encryption_key))
    finally:
        await db.dispose()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(_main(args))
    except UsageError as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
