"""The API's /health (P7-4, D62): Docker's check that the API, its database and Redis
answer. Public, so it says only ok or down. Also the smoke test that backend.main starts
with a valid config."""

import importlib
from collections.abc import AsyncIterator, Iterator

import httpx2
import pytest
import structlog
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import URL
from structlog.testing import capture_logs

from backend.app import create_app
from backend.testing import Api, api_settings
from shared.db.session import make_engine

NOWHERE = "redis://127.0.0.1:1/0"  # nothing listens on port 1: refused at once


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # backend.main loads settings at import, so the required ones must be set first.
    monkeypatch.setenv("POSTGRES_USER", "crypto")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret-test-password")
    monkeypatch.setenv("POSTGRES_DB", "crypto_trading")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SENTRY_DSN", "")
    # No database or Redis there, so /health says so, quickly.
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "1")
    monkeypatch.setenv("REDIS_URL", NOWHERE)
    main = importlib.import_module("backend.main")
    main = importlib.reload(main)  # with this test's environment, if imported before
    with TestClient(main.app) as client:  # `with` runs the lifespan, which opens both
        yield client
    # Importing it configured structlog for the whole process (INFO and up), which
    # would hide debug logs from later tests' capture_logs: put the defaults back.
    structlog.reset_defaults()


def test_the_api_starts_and_its_health_says_what_is_down_without_why(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "down", "db": "down", "redis": "down"}


async def test_health_is_ok_with_the_database_and_redis_up(api: Api) -> None:
    response = await api.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "redis": "ok"}


@pytest.fixture
async def broken(db_url: URL, redis_url: str) -> AsyncIterator[tuple[FastAPI, httpx2.AsyncClient]]:
    """The API with its database somewhere that refuses, and Redis up."""
    app = create_app(api_settings(redis_url), database_url=db_url.set(port=1, host="127.0.0.1"))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://testserver"
        ) as client,
    ):
        yield app, client


async def test_a_database_down_is_503_logged_once_and_back_up_is_logged_too(
    broken: tuple[FastAPI, httpx2.AsyncClient], db_url: URL
) -> None:
    app, client = broken

    with capture_logs() as logs:
        first = await client.get("/health")
        await client.get("/health")  # still down: not logged again

    assert first.status_code == 503
    assert first.json() == {"status": "down", "db": "down", "redis": "ok"}
    failed = [log for log in logs if log["event"] == "api.health.failed"]
    assert len(failed) == 1
    assert failed[0]["down"] == ["db"] and failed[0]["log_level"] == "warning"

    # The database is back.
    await app.state.db.dispose()
    app.state.db = make_engine(db_url)
    with capture_logs() as logs:
        again = await client.get("/health")

    assert again.status_code == 200
    assert [log["event"] for log in logs if log["event"].startswith("api.health")] == [
        "api.health.recovered"
    ]


async def test_redis_down_is_503(api: Api) -> None:
    up = api.app.state.redis
    api.app.state.redis = Redis.from_url(NOWHERE)
    try:
        response = await api.client.get("/health")
    finally:
        await api.app.state.redis.aclose()
        api.app.state.redis = up

    assert response.status_code == 503
    assert response.json() == {"status": "down", "db": "ok", "redis": "down"}


@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        ("JWT_SECRET", "too short", "JWT_SECRET: Value error, must be at least 32 characters"),
        ("ENCRYPTION_KEY", "not-a-key", "ENCRYPTION_KEY: Value error, must be a Fernet key"),
    ],
)
def test_a_bad_secret_stops_the_api_with_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, problem: str
) -> None:
    from backend.settings import load_api_settings

    for key, good in {
        "POSTGRES_USER": "crypto",
        "POSTGRES_PASSWORD": "pw",
        "POSTGRES_DB": "db",
        "JWT_SECRET": "x" * 32,
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setenv(key, good)
    monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as stopped:
        load_api_settings(env_file=None)

    message = str(stopped.value)
    assert problem in message
    assert value not in message.replace(problem, "")  # the secret itself is never echoed
