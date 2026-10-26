"""Checking a sign-in, and the `users` rows behind it.

A sign-in needs the username, the password and a current TOTP code, all three; any
wrong part gets the same answer, so a guess learns nothing about which part was wrong.
Each one is decided in a transaction holding the user's row (`FOR UPDATE`), so two
attempts at once can't both use the same code or both slip under the lockout.

Lockout: `MAX_FAILURES` wrong attempts in a row lock the user for `LOCK_MINUTES`.
While locked, nothing is checked at all, so guesses get no answer either way. Once the
lock has passed, the count starts again. A right sign-in resets it.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.auth.crypto import Secrets
from backend.auth.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from backend.auth.totp import accepted_step
from shared.db.models import UserRow

MAX_FAILURES = 5
LOCK_MINUTES = 15


@dataclass(frozen=True)
class SignedIn:
    user_id: int
    username: str
    session_version: int


@dataclass(frozen=True)
class Wrong:
    """The username, password or code was wrong (we don't say which)."""


@dataclass(frozen=True)
class Locked:
    until: datetime


@dataclass(frozen=True)
class SessionUser:
    """Who a session belongs to, as the API checks it on every request."""

    id: int
    username: str
    session_version: int


async def sign_in(
    db: AsyncEngine, secrets: Secrets, username: str, password: str, code: str, now: datetime
) -> SignedIn | Wrong | Locked:
    async with db.begin() as conn:
        user = (
            await conn.execute(
                select(UserRow.__table__).where(UserRow.username == username).with_for_update()
            )
        ).one_or_none()
        if user is None:
            # As long as a real check, so the timing doesn't say the username is wrong.
            await asyncio.to_thread(verify_password, DUMMY_HASH, password)
            return Wrong()
        if user.locked_until is not None and user.locked_until > now:
            return Locked(user.locked_until)
        # argon2 takes tens of milliseconds of CPU: in a thread, so other requests go on.
        password_ok = await asyncio.to_thread(verify_password, user.password_hash, password)
        step = None
        if password_ok:
            secret = secrets.decrypt(user.totp_secret_enc)
            step = accepted_step(secret, code, now, user.totp_last_step)
        if step is None:
            lock_passed = user.locked_until is not None  # (and not in force: checked above)
            failures = 1 if lock_passed else user.failed_logins + 1
            locked = now + timedelta(minutes=LOCK_MINUTES) if failures >= MAX_FAILURES else None
            await conn.execute(
                update(UserRow)
                .where(UserRow.id == user.id)
                .values(failed_logins=failures, locked_until=locked, updated_at=now)
            )
            return Wrong() if locked is None else Locked(locked)
        values: dict[str, object] = {
            "failed_logins": 0,
            "locked_until": None,
            "totp_last_step": step,
            "updated_at": now,
        }
        if needs_rehash(user.password_hash):  # the library's settings moved on
            values["password_hash"] = await asyncio.to_thread(hash_password, password)
        await conn.execute(update(UserRow).where(UserRow.id == user.id).values(values))
        return SignedIn(user.id, user.username, user.session_version)


async def session_user(db: AsyncEngine, user_id: int) -> SessionUser | None:
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(UserRow.id, UserRow.username, UserRow.session_version).where(
                    UserRow.id == user_id
                )
            )
        ).one_or_none()
    return None if row is None else SessionUser(row.id, row.username, row.session_version)


async def end_sessions(db: AsyncEngine, user_id: int, now: datetime) -> None:
    """End every session of the user: tokens carry the old version from now on."""
    async with db.begin() as conn:
        await conn.execute(
            update(UserRow)
            .where(UserRow.id == user_id)
            .values(session_version=UserRow.session_version + 1, updated_at=now)
        )
