"""Tests for shared.config. They never read the real .env (env_file=None)."""

import pytest

from shared.config import load_settings

PASSWORD = "s3cret-test-password"
CONFIG_VARS = (
    "MODE",
    "LOG_LEVEL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "REDIS_URL",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Start from a clean environment with only the required settings set."""
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSTGRES_USER", "crypto")
    monkeypatch.setenv("POSTGRES_PASSWORD", PASSWORD)
    monkeypatch.setenv("POSTGRES_DB", "crypto_trading")
    return monkeypatch


def test_defaults_are_paper_and_docker_hosts(env: pytest.MonkeyPatch) -> None:
    settings = load_settings(env_file=None)

    assert settings.mode == "paper"
    assert settings.log_level == "INFO"
    assert settings.postgres_host == "db"
    assert settings.postgres_port == 5432
    assert settings.redis_url == "redis://redis:6379/0"
    assert settings.postgres_password.get_secret_value() == PASSWORD


def test_missing_required_setting_names_it(env: pytest.MonkeyPatch) -> None:
    env.delenv("POSTGRES_PASSWORD")

    with pytest.raises(SystemExit) as exc:
        load_settings(env_file=None)

    message = str(exc.value)
    assert "POSTGRES_PASSWORD" in message
    assert ".env.example" in message


def test_invalid_mode_names_it(env: pytest.MonkeyPatch) -> None:
    env.setenv("MODE", "lve")

    with pytest.raises(SystemExit) as exc:
        load_settings(env_file=None)

    assert "MODE" in str(exc.value)


def test_password_never_shown(env: pytest.MonkeyPatch) -> None:
    settings = load_settings(env_file=None)
    assert PASSWORD not in repr(settings)
    assert PASSWORD not in str(settings)

    # An invalid value must not be echoed back in the error either.
    env.setenv("POSTGRES_PORT", PASSWORD)
    with pytest.raises(SystemExit) as exc:
        load_settings(env_file=None)
    assert PASSWORD not in str(exc.value)
