"""The engine loop writes its heartbeat file, which Docker's health check watches."""

import asyncio
from pathlib import Path

import pytest

from engine import main


async def test_heartbeat_writes_its_file_until_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "engine-heartbeat"
    monkeypatch.setattr(main, "HEARTBEAT_FILE", heartbeat)
    stop = asyncio.Event()

    task = asyncio.create_task(main.heartbeat(stop))
    # The first beat happens right away; the timeout only guards against a hang.
    async with asyncio.timeout(5):
        while not heartbeat.exists():  # noqa: ASYNC110 (polling a file, not an event)
            await asyncio.sleep(0.01)

    stop.set()
    async with asyncio.timeout(5):
        await task  # returns promptly, without waiting out the interval
