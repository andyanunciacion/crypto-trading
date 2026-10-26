"""Smoke test for the API: the app starts with a valid config and /health answers."""

import importlib
from collections.abc import Iterator

import pytest
import structlog
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # backend.main loads settings at import, so the required ones must be set first.
    monkeypatch.setenv("POSTGRES_USER", "crypto")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret-test-password")
    monkeypatch.setenv("POSTGRES_DB", "crypto_trading")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    main = importlib.import_module("backend.main")
    yield TestClient(main.app)
    # Importing it configured structlog for the whole process (INFO and up), which
    # would hide debug logs from later tests' capture_logs: put the defaults back.
    structlog.reset_defaults()


def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
