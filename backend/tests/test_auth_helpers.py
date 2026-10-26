"""The pure parts of auth (backend/auth/): password hashes, TOTP codes, session tokens and
encryption at rest. No database, no clock: each takes the time it's given."""

from datetime import UTC, datetime, timedelta

import jwt
import pyotp
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from backend.auth.crypto import Secrets
from backend.auth.passwords import hash_password, needs_rehash, verify_password
from backend.auth.tokens import Session, decode_session, issue_session
from backend.auth.totp import STEP_SECONDS, accepted_step, new_secret, provisioning_uri

NOW = datetime(2026, 10, 26, 12, 0, 15, tzinfo=UTC)
JWT_SECRET = SecretStr("x" * 32)


# --- Passwords ---------------------------------------------------------------------------


def test_a_password_verifies_against_its_hash_only() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert hashed != hash_password("correct horse battery")  # a new salt each time
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "correct horse batterY")
    assert not needs_rehash(hashed)


def test_a_broken_hash_never_verifies() -> None:
    assert not verify_password("not a hash", "anything")


# --- TOTP --------------------------------------------------------------------------------


def code_at(secret: str, when: datetime) -> str:
    return pyotp.TOTP(secret).at(when)


def test_the_current_code_is_accepted_and_names_its_step() -> None:
    secret = new_secret()
    step = int(NOW.timestamp()) // STEP_SECONDS
    assert accepted_step(secret, code_at(secret, NOW), NOW, last_step=None) == step


@pytest.mark.parametrize("offset", [-1, 1])
def test_a_code_one_step_off_is_accepted_for_clock_drift(offset: int) -> None:
    secret = new_secret()
    when = NOW + timedelta(seconds=offset * STEP_SECONDS)
    step = int(NOW.timestamp()) // STEP_SECONDS + offset
    assert accepted_step(secret, code_at(secret, when), NOW, last_step=None) == step


def test_a_code_two_steps_off_is_refused() -> None:
    secret = new_secret()
    old = code_at(secret, NOW - timedelta(seconds=2 * STEP_SECONDS))
    assert accepted_step(secret, old, NOW, last_step=None) is None


def test_a_code_already_used_is_refused() -> None:
    secret = new_secret()
    code = code_at(secret, NOW)
    step = accepted_step(secret, code, NOW, last_step=None)
    assert step is not None
    # The same code again, or an earlier one, after it was accepted: a replay.
    assert accepted_step(secret, code, NOW, last_step=step) is None
    earlier = code_at(secret, NOW - timedelta(seconds=STEP_SECONDS))
    assert accepted_step(secret, earlier, NOW, last_step=step) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", " 123456"])
def test_a_malformed_code_is_refused(code: str) -> None:
    assert accepted_step(new_secret(), code, NOW, last_step=None) is None


def test_the_provisioning_uri_names_the_app_and_user() -> None:
    uri = provisioning_uri("JBSWY3DPEHPK3PXP", "andy")
    assert uri.startswith("otpauth://totp/crypto-trading:andy?")
    assert "secret=JBSWY3DPEHPK3PXP" in uri


# --- Session tokens ----------------------------------------------------------------------


def test_a_session_round_trips_until_it_expires() -> None:
    token = issue_session(7, 3, JWT_SECRET, NOW, timedelta(hours=12))
    expected = Session(user_id=7, version=3, expires_at=NOW + timedelta(hours=12))
    assert decode_session(token, JWT_SECRET, NOW) == expected
    assert decode_session(token, JWT_SECRET, NOW + timedelta(hours=11, minutes=59)) == expected
    assert decode_session(token, JWT_SECRET, NOW + timedelta(hours=12)) is None


def test_a_token_issued_in_the_future_is_refused() -> None:
    token = issue_session(7, 3, JWT_SECRET, NOW + timedelta(hours=1), timedelta(hours=12))
    assert decode_session(token, JWT_SECRET, NOW) is None


def test_a_token_signed_with_another_secret_or_tampered_with_is_refused() -> None:
    token = issue_session(7, 3, JWT_SECRET, NOW, timedelta(hours=12))
    assert decode_session(token, SecretStr("y" * 32), NOW) is None
    header, payload, signature = token.split(".")
    assert decode_session(f"{header}.{payload}x.{signature}", JWT_SECRET, NOW) is None
    assert decode_session("garbage", JWT_SECRET, NOW) is None


def test_an_unsigned_token_is_refused() -> None:
    # alg "none": a classic way to skip the signature check.
    unsigned = jwt.encode({"sub": "7", "ver": 3, "iat": 0, "exp": 2**40}, key="", algorithm="none")
    assert decode_session(unsigned, JWT_SECRET, NOW) is None


# --- Encryption at rest ------------------------------------------------------------------


def test_secrets_are_encrypted_and_come_back() -> None:
    secrets = Secrets(SecretStr(Fernet.generate_key().decode()))
    sealed = secrets.encrypt("JBSWY3DPEHPK3PXP")
    assert "JBSWY3DPEHPK3PXP" not in sealed
    assert secrets.decrypt(sealed) == "JBSWY3DPEHPK3PXP"
