"""The FastAPI app's routes and how they're guarded (main.py builds it from the settings).

Every route needs a signed-in session (backend/auth/) unless its path is in `PUBLIC`.
New routes are included into the `protected` router, which checks the session before
anything else runs, so the check can't be forgotten on one route.
test_every_route_needs_auth.py calls every route without a session and fails on any
that answers.

The dashboard reaches the API under `/api` on its own origin (Next.js rewrites strip
the prefix), so the browser sends the session cookie with no CORS involved (D44).
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from sqlalchemy import URL

from backend.auth import routes as auth_routes
from backend.auth.crypto import Secrets
from backend.auth.deps import current_user
from backend.bots import routes as bot_routes
from backend.dashboard import routes as dashboard_routes
from backend.market import routes as market_routes
from backend.settings import ApiSettings
from shared.db.session import make_engine

# The only routes anyone can call without signing in.
PUBLIC = frozenset({"/health", "/auth/login"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


public = APIRouter()


@public.get("/health")
async def health() -> dict[str, str]:
    """Liveness check used by Docker. DB and Redis checks are added in P7-4."""
    return {"status": "ok"}


api_docs = APIRouter(include_in_schema=False)


@api_docs.get("/openapi.json")
async def openapi(request: Request) -> dict[str, Any]:
    schema: dict[str, Any] = request.app.openapi()
    return schema


@api_docs.get("/docs")
async def docs() -> HTMLResponse:
    # Relative, so it works both at /docs and through the dashboard at /api/docs.
    return get_swagger_ui_html(openapi_url="openapi.json", title="crypto-trading API")


def create_app(
    settings: ApiSettings,
    *,
    database_url: URL | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """The API. `database_url` and `clock` are for tests; otherwise the settings decide."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = make_engine(database_url or settings.database_url)
        # Live prices, the engine's heartbeat, and commands to the engine (D45).
        app.state.redis = Redis.from_url(settings.redis_url)
        try:
            yield
        finally:
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

    app.include_router(public)
    app.include_router(auth_routes.public)
    app.include_router(protected)
    return app
