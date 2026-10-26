"""`/auth`: sign in (public), sign out and who's signed in (both need a session).

A sign-in sets the `session` cookie:
- `HttpOnly`: page scripts can't read it, so injected JavaScript can't steal it;
- `Secure`: sent over HTTPS only (and http://localhost, which browsers treat as secure);
- `SameSite=Strict`: never sent on a request another site starts. With JSON-only
  bodies, that's what stops cross-site request forgery (CSRF).
"""

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.auth.crypto import Secrets
from backend.auth.deps import SESSION_COOKIE, CurrentUser
from backend.auth.login import Locked, Wrong, end_sessions, sign_in
from backend.auth.tokens import issue_session
from backend.settings import ApiSettings

log = structlog.get_logger()

public = APIRouter(prefix="/auth", tags=["auth"])
router = APIRouter(prefix="/auth", tags=["auth"])


class SignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(max_length=16)  # the TOTP code; checked for 6 digits later


class Me(BaseModel):
    username: str


def _state(request: Request) -> tuple[ApiSettings, AsyncEngine, Secrets, datetime]:
    state = request.app.state
    clock = cast(Callable[[], datetime], state.clock)
    return cast(ApiSettings, state.settings), state.db, cast(Secrets, state.secrets), clock()


@public.post("/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(body: SignIn, request: Request, response: Response) -> None:
    settings, db, secrets, now = _state(request)
    result = await sign_in(db, secrets, body.username, body.password, body.code, now)
    # Never the password or the code (§10).
    fields = {"username": body.username, "ip": request.client.host if request.client else None}
    if isinstance(result, Locked):
        wait = max(1, math.ceil((result.until - now).total_seconds()))
        log.warning("auth.login", outcome="locked", until=result.until.isoformat(), **fields)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many failed sign-ins: try again in {math.ceil(wait / 60)} min",
            headers={"Retry-After": str(wait)},
        )
    if isinstance(result, Wrong):
        log.warning("auth.login", outcome="wrong", **fields)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong username, password or code")
    lifetime = timedelta(hours=settings.session_hours)
    token = issue_session(
        result.user_id, result.session_version, settings.jwt_secret, now, lifetime
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(lifetime.total_seconds()),
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )
    log.info("auth.login", outcome="ok", user_id=result.user_id, **fields)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, user: CurrentUser) -> None:
    """Sign out everywhere: every session of the user ends, not only this browser's."""
    settings, db, _, now = _state(request)
    await end_sessions(db, user.id, now)
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, secure=settings.cookie_secure, samesite="strict"
    )
    log.info("auth.logout", user_id=user.id, username=user.username)


@router.get("/me")
async def me(user: CurrentUser) -> Me:
    return Me(username=user.username)
