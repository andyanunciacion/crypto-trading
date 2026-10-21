"""Smoke test for the API: the app starts with a valid config and /health answers."""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # backend.main loads settings at import, so the required ones must be set first.
    monkeypatch.setenv("POSTGRES_USER", "crypto")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret-test-password")
    monkeypatch.setenv("POSTGRES_DB", "crypto_trading")
    main = importlib.import_module("backend.main")
    return TestClient(main.app)


def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
