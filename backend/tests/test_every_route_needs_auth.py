"""Every API route refuses a caller who isn't signed in, unless it's in `PUBLIC` (D44).

It walks the real app's routes, so a route added later is checked too: one that
answers without a session fails here unless someone deliberately makes it public.
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIWebSocketRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from backend.app import PUBLIC
from backend.testing import Api


def test_only_health_and_sign_in_are_public() -> None:
    assert frozenset({"/health", "/auth/login"}) == PUBLIC


def assert_websocket_refused(app: FastAPI, path: str) -> None:
    """A WebSocket without a session is closed with 1008 (policy violation)."""
    # Starlette's TestClient speaks WebSocket; it runs the app (and its lifespan) itself.
    with (
        TestClient(app, base_url="https://testserver") as client,
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect(path) as socket,
    ):
        socket.receive_text()
    assert closed.value.code == 1008, f"WebSocket {path} wasn't refused"


async def test_every_other_route_refuses_a_caller_without_a_session(api: Api) -> None:
    checked: list[str] = []
    # Included routers stay nested in app.routes; this lists each route with its full path.
    for route in iter_route_contexts(api.app.routes):
        if route.path is None or route.path in PUBLIC:
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)  # any value for a path parameter
        if isinstance(route.original_route, APIWebSocketRoute | WebSocketRoute):
            assert_websocket_refused(api.app, path)
            checked.append(f"WS {route.path}")
            continue
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue
            response = await api.client.request(method, path, json={})
            assert response.status_code == 401, f"{method} {route.path} answered without a session"
            checked.append(f"{method} {route.path}")

    # The routes there are today, so the walk can't silently check nothing.
    assert {
        "GET /auth/me",
        "POST /auth/logout",
        "GET /docs",
        "GET /openapi.json",
        "GET /overview",
        "WS /ws/overview",
        "POST /engine/kill",
        "GET /bots/{bot_id}/chart",
        "WS /ws/bots/{bot_id}/chart",
        "GET /orders/{order_id}/explain",
        "GET /strategies",
        "GET /bots",
        "POST /bots",
        "POST /bots/{bot_id}/start",
        "POST /bots/{bot_id}/pause",
        "POST /bots/{bot_id}/stop",
        "POST /bots/{bot_id}/strategy",
        "GET /bots/{bot_id}",
        "POST /bots/{bot_id}/rollback",
        "POST /backtests",
        "GET /backtests/jobs/{job_id}",
        "GET /backtests",
        "GET /backtests/{run_id}",
        "GET /candles/range",
        "GET /history/orders",
        "GET /history/orders.csv",
        "GET /history/fills",
        "GET /history/fills.csv",
        "GET /logs",
        "GET /risk-events",
        "GET /risk-settings",
        "PUT /risk-settings/{mode}",
        "GET /exchange-accounts",
        "POST /exchange-accounts",
        "PUT /exchange-accounts/{account_id}",
        "DELETE /exchange-accounts/{account_id}",
    } <= set(checked)


async def test_the_public_routes_answer_without_a_session(api: Api) -> None:
    assert (await api.client.get("/health")).status_code == 200
    assert (await api.login()).status_code == 204


async def test_the_docs_open_once_signed_in(api: Api) -> None:
    await api.login()
    docs = await api.client.get("/docs")
    schema = await api.client.get("/openapi.json")

    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    assert schema.status_code == 200 and "/auth/login" in schema.json()["paths"]
