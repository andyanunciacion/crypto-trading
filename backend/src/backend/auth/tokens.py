"""Session tokens: a JWT the API signs at login and the browser keeps in a cookie.

A JWT is a small JSON payload plus a signature (HMAC-SHA256 with `JWT_SECRET`). The API
can tell it made the token without storing it; changing one byte breaks the signature.
The payload says who (`sub`), which session version (`ver`: logout bumps the user's,
ending every token issued before) and until when (`exp`).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import SecretStr

ALGORITHM = "HS256"
CLOCK_LEEWAY_SECONDS = 60  # only the API issues tokens, so its own clock; a little slack


@dataclass(frozen=True)
class Session:
    user_id: int
    version: int
    expires_at: datetime


def issue_session(
    user_id: int, version: int, secret: SecretStr, now: datetime, lifetime: timedelta
) -> str:
    claims = {
        "sub": str(user_id),  # the JWT standard wants a string
        "ver": version,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(claims, secret.get_secret_value(), algorithm=ALGORITHM)


def decode_session(token: str, secret: SecretStr, now: datetime) -> Session | None:
    """The session in `token`, or None if it's forged, malformed or expired at `now`."""
    try:
        claims = jwt.decode(
            token,
            secret.get_secret_value(),
            algorithms=[ALGORITHM],  # only ours: never "none", never one the token picks
            # The times are checked below, against `now`, so tests can set the time.
            options={
                "require": ["sub", "ver", "iat", "exp"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
        user_id = int(claims["sub"])
        version = claims["ver"]
        iat = claims["iat"]
        exp = claims["exp"]
    except (jwt.InvalidTokenError, ValueError):
        return None
    if not all(isinstance(value, int) for value in (version, iat, exp)):
        return None
    expires_at = datetime.fromtimestamp(exp, UTC)
    if now >= expires_at or iat > now.timestamp() + CLOCK_LEEWAY_SECONDS:
        return None  # expired, or issued in the future
    return Session(user_id=user_id, version=version, expires_at=expires_at)
