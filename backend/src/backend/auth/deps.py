"""`current_user`: the FastAPI dependency that guards every route but the public ones.

It reads the `session` cookie, checks the JWT's signature and expiry, and checks that
the user still exists with the same session version (logout bumps it). Anything wrong
is a 401 for a request, or a closed connection (1008, policy violation) for a
WebSocket. The same dependency serves both, because a WebSocket's opening handshake is
an HTTP request that carries the cookie.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, cast

from fastapi import Depends, HTTPException, WebSocketException, status
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from backend.auth.login import SessionUser, session_user
from backend.auth.tokens import decode_session
from backend.settings import ApiSettings

SESSION_COOKIE = "session"


async def signed_in(conn: HTTPConnection) -> SessionUser | None:
    """The user whose session `conn` carries, or None if it carries no valid one.

    A WebSocket asks again while it's open (dashboard/routes.py): its cookie was read
    once, at the handshake, but logout or expiry must still end it.
    """
    state = conn.app.state
    settings = cast(ApiSettings, state.settings)
    clock = cast(Callable[[], datetime], state.clock)
    token = conn.cookies.get(SESSION_COOKIE)
    session = None if token is None else decode_session(token, settings.jwt_secret, clock())
    user = None if session is None else await session_user(state.db, session.user_id)
    if user is None or session is None or user.session_version != session.version:
        return None
    return user


async def current_user(conn: HTTPConnection) -> SessionUser:
    user = await signed_in(conn)
    if user is None:
        if isinstance(conn, WebSocket):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="sign in")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not signed in")
    return user


# For a route that needs to know who's signed in: `user: CurrentUser`.
CurrentUser = Annotated[SessionUser, Depends(current_user)]
