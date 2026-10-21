"""The engine loop writes its heartbeat file, which Docker's health check watches."""

import asyncio
from pathlib import Path

import pytest

from engine import main
from shared.config import Settings, load_settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("POSTGRES_USER", "crypto")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret-test-password")
    monkeypatch.setenv("POSTGRES_DB", "crypto_trading")
    return load_settings(env_file=None)


async def test_run_writes_heartbeat(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "engine-heartbeat"
    monkeypatch.setattr(main, "HEARTBEAT_FILE", heartbeat)

    task = asyncio.create_task(main.run(settings))
    # The first beat happens right away; the timeout only guards against a hang.
    async with asyncio.timeout(5):
        while not heartbeat.exists():  # noqa: ASYNC110 (polling a file, not an event)
            await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
