"""TOTP: the 6-digit code an authenticator app shows, new every 30 s (RFC 6238).

The app and the server share a secret; each derives the code from it and the current
30-second "step". A code from one step either side is accepted, for clock drift
between the phone and the server. A code is only good once: the step of the last one
accepted is stored, and that step or any earlier one is refused, so a code seen over
someone's shoulder can't be used again.
"""

import hmac
import re
from datetime import datetime

import pyotp

STEP_SECONDS = 30
ISSUER = "crypto-trading"
_CODE = re.compile(r"\d{6}")


def new_secret() -> str:
    """A random base32 secret (160 bits), as authenticator apps expect."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    """The `otpauth://` URI an authenticator app reads from the QR code."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def accepted_step(secret: str, code: str, now: datetime, last_step: int | None) -> int | None:
    """The step `code` belongs to, if it's valid at `now` (one step either side) and
    newer than `last_step`, the step of the last code accepted. None otherwise."""
    if not _CODE.fullmatch(code):
        return None
    totp = pyotp.TOTP(secret)
    current = int(now.timestamp()) // STEP_SECONDS
    for step in (current, current - 1, current + 1):
        if last_step is not None and step <= last_step:
            continue
        # Constant time: how much of the code matched mustn't show in the timing.
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), code):
            return step
    return None
