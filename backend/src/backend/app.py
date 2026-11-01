"""The FastAPI app's routes and how they're guarded (main.py builds it from the settings).

Every route needs a signed-in session (backend/auth/) unless its path is in `PUBLIC`.
New routes are included into the `protected` router, which checks the session before
anything else runs, so the check can't be forgotten on one route.
test_every_route_needs_auth.py calls every route without a session and fails on any
that answers.

The dashboard reaches the API under `/api` on its own origin (Next.js rewrites strip
the prefix), so the browser sends the session cookie with no CORS involved (D44).
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from redis.asyncio import Redis
from sqlalchemy import URL

from backend.alert_settings import routes as alert_settings_routes
from backend.auth import routes as auth_routes
from backend.auth.confirm import CodeRefused, handle_code_refused
from backend.auth.crypto import Secrets
from backend.auth.deps import current_user
from backend.backtests import routes as backtest_routes
from backend.bots import routes as bot_routes
from backend.dashboard import routes as dashboard_routes
from backend.health import routes as health_routes
from backend.history import routes as history_routes
from backend.keys import routes as key_routes
from backend.logs import routes as log_routes
from backend.market import routes as market_routes
from backend.risk_settings import routes as risk_settings_routes
from backend.settings import ApiSettings
from shared.db.session import make_engine
from shared.logs import LogMirror

# The only routes anyone can call without signing in.
PUBLIC = frozenset({"/health", "/auth/login"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


api_docs = APIRouter(include_in_schema=False)


@api_docs.get("/openapi.json")
async def openapi(request: Request) -> dict[str, Any]:
    schema: dict[str, Any] = request.app.openapi()
    return schema


@api_docs.get("/docs")
async def docs() -> HTMLResponse:
    # Relative, so it works both at /docs and through the dashboard at /api/docs.
    return get_swagger_ui_html(openapi_url="openapi.json", title="crypto-trading API")


async def _validation_failed(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422, without the `input` it normally echoes back: that could be an
    exchange secret or a password. `loc` and `msg` say what's wrong (D50)."""
    assert isinstance(exc, RequestValidationError)
    errors = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
    return JSONResponse(
        {"detail": jsonable_encoder(errors)}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
    )


def create_app(
    settings: ApiSettings,
    *,
    database_url: URL | None = None,
    clock: Callable[[], datetime] = _utcnow,
    mirror: LogMirror | None = None,
) -> FastAPI:
    """The API. `database_url` and `clock` are for tests; otherwise the settings decide.
    With a `mirror`, the API's log lines go to the Logs page's stream too (D49)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = make_engine(database_url or settings.database_url)
        # Live prices, the engine's heartbeat, commands to the engine (D45), the logs (D49).
        app.state.redis = Redis.from_url(settings.redis_url)
        stop = asyncio.Event()
        shipping = (
            None if mirror is None else asyncio.create_task(mirror.ship(app.state.redis, stop))
        )
        try:
            yield
        finally:
            stop.set()
            if shipping is not None:
                await shipping  # a last flush
            await app.state.redis.aclose()
            await app.state.db.dispose()

    # FastAPI serves its docs to anyone by default: off here, and served behind sign-in.
    app = FastAPI(
        title="crypto-trading API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.secrets = Secrets(settings.encryption_key)
    app.state.clock = clock

    protected = APIRouter(dependencies=[Depends(current_user)])
    protected.include_router(api_docs)
    protected.include_router(auth_routes.router)
    protected.include_router(dashboard_routes.router)
    protected.include_router(market_routes.router)
    protected.include_router(bot_routes.router)
    protected.include_router(backtest_routes.router)
    protected.include_router(history_routes.router)
    protected.include_router(log_routes.router)
    protected.include_router(risk_settings_routes.router)
    protected.include_router(key_routes.router)
    protected.include_router(alert_settings_routes.router)
    protected.include_router(health_routes.router)

    app.add_exception_handler(RequestValidationError, _validation_failed)
    app.add_exception_handler(CodeRefused, handle_code_refused)
    app.include_router(health_routes.public)
    app.include_router(auth_routes.public)
    app.include_router(protected)
    return app
