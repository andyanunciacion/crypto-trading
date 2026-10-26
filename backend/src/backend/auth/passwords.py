"""Password hashes: argon2id, a slow, memory-hard hash made for passwords.

"Slow" is the point: each guess costs an attacker real time and memory, even with the
hash in hand. Each hash carries its own random salt and settings, so the same
password hashes differently every time, and `needs_rehash` says when the library's
defaults have moved on.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 12

_hasher = PasswordHasher()  # argon2id with the library's recommended settings


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Whether `password` matches. Never raises: a broken hash just doesn't match."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# Checked when the username doesn't exist, so a wrong username takes as long as a wrong
# password and the timing doesn't tell which usernames exist.
DUMMY_HASH = hash_password("no user has this password, it only costs the same time")
