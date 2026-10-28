"""Asking a signed-in user for a TOTP code again, before something that adds risk (D50).

Loosening a risk limit (UX4) or saving exchange keys needs a current code from the
authenticator app, on top of the session: a stolen cookie alone can't do either. The
code is checked by `confirm_code` (login.py), in the same transaction as the change.

`code_refusal` gives back a `CodeRefused` instead of raising it: a wrong code's failure
(it counts toward the sign-in lockout) is written in that transaction, so the caller
lets the transaction commit, then raises it. `handle_code_refused` turns it into the
answer: 403 with `needs_code` (and whatever the route adds, like what an edit loosens),
or 429 with `Retry-After` while locked. Never 401: the dashboard reads a 401 as
"signed out" and goes to the sign-in page.
"""

import math
from datetime import datetime

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncConnection

from backend.auth.crypto import Secrets
from backend.auth.login import Locked, Wrong, confirm_code

WRONG_CODE = "wrong or already used code: wait for the app's next one"


class CodeRefused(Exception):
    """A change that needs a TOTP code didn't come with a right one."""

    def __init__(self, status_code: int, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after
        self.extra: dict[str, JsonValue] = {}  # more for the dashboard, e.g. `loosened`


async def code_refusal(
    conn: AsyncConnection,
    secrets: Secrets,
    user_id: int,
    code: str | None,
    now: datetime,
    *,
    needed_for: str,
) -> CodeRefused | None:
    """None if `code` is the user's right, unused code; otherwise why not. `needed_for`
    says what asked for it ("raising a risk limit")."""
    if code is None:
        return CodeRefused(
            status.HTTP_403_FORBIDDEN, f"{needed_for} needs a code from your authenticator app"
        )
    result = await confirm_code(conn, secrets, user_id, code, now)
    if isinstance(result, Locked):
        wait = max(1, math.ceil((result.until - now).total_seconds()))
        return CodeRefused(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many wrong codes or sign-ins: try again in {math.ceil(wait / 60)} min",
            retry_after=wait,
        )
    if isinstance(result, Wrong):
        return CodeRefused(status.HTTP_403_FORBIDDEN, WRONG_CODE)
    return None


async def handle_code_refused(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CodeRefused)
    headers = None if exc.retry_after is None else {"Retry-After": str(exc.retry_after)}
    return JSONResponse(
        {"detail": exc.detail, "needs_code": True, **exc.extra},
        status_code=exc.status_code,
        headers=headers,
    )
