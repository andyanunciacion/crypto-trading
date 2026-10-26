"""A pytest plugin with fixtures for every package's tests. Test-only: never import it in app code.

pyproject.toml loads it with `-p shared.testing`. It lives here rather than in a root
conftest.py because mypy can't check two modules both named `conftest`.

`timescale_url` and `redis_url` each start a throwaway container (the same images as
compose.yaml) once per test run, via testcontainers (a dev dependency), and remove it
afterwards. They need Docker: without it, those tests are skipped locally, but fail in
CI so they can't quietly stop running.
"""

import os
import shutil
import subprocess
from collections.abc import Iterator

import pytest
from sqlalchemy import URL, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from shared.db.migrate import upgrade
from shared.db.models import BotRow, RiskSettingsRow

# Keep both in step with compose.yaml.
TIMESCALE_IMAGE = "timescale/timescaledb:2.31.0-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"


def _docker_running() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _require_docker() -> None:
    if not _docker_running():
        reason = "database and Redis tests need Docker; start Docker Desktop"
        if os.environ.get("CI"):
            pytest.fail(reason)
        pytest.skip(reason)


@pytest.fixture(scope="session")
def timescale_url() -> Iterator[URL]:
    """An empty TimescaleDB database, shared by the whole test run."""
    _require_docker()
    container = PostgresContainer(
        TIMESCALE_IMAGE, username="test", password="test", dbname="test", driver=None
    ).with_env("TIMESCALEDB_TELEMETRY", "off")
    with container:
        yield URL.create(
            "postgresql+asyncpg",
            username="test",
            password="test",
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(5432),
            database="test",
        )


@pytest.fixture(scope="session")
def db_url(timescale_url: URL) -> URL:
    """The test database with every migration applied."""
    upgrade(timescale_url)
    return timescale_url


async def insert_bot(db: AsyncEngine, bot_id: int = 1) -> None:
    """Make sure bot `bot_id` exists and is running, for tests that write orders or
    balances for it.

    Those tables' `bot_id` must name a real bot, and the RiskManager blocks the buys of
    one that isn't running (D42), so a bot an earlier test paused or stopped is set
    running again.
    Tests that truncate `bots` should `RESTART IDENTITY`: this sets the id itself, which
    doesn't move the id sequence on.
    """
    row = {
        "id": bot_id,
        "name": f"test-bot-{bot_id}",
        "exchange": "mexc",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "status": "running",
    }
    running = {
        "status": "running",
        "paused_reason": None,
        "paused_at": None,
        "stopped_reason": None,
        "stopped_at": None,
    }
    async with db.begin() as conn:
        await conn.execute(
            insert(BotRow).values(row).on_conflict_do_update(index_elements=["id"], set_=running)
        )


async def set_limits(db: AsyncEngine, mode: str = "paper", **limits: object) -> None:
    """Set `mode`'s row in `risk_settings` to exactly `limits`; any left out are off.

    The test database is shared by the whole run, so tests whose orders go through a
    RiskManager set the limits they need instead of relying on what's there. With no
    `limits`, every limit and circuit breaker is off: for tests that aren't about them.
    """
    row = {
        "max_open_positions": None,
        "max_coin_exposure_pct": None,
        "max_daily_loss_pct": None,
        "max_drawdown_pct": None,
        "stale_after_candles": None,
        "max_failed_orders": None,
        "failed_orders_minutes": None,
        "spike_pct": None,
        "spike_minutes": None,
        **limits,
    }
    async with db.begin() as conn:
        await conn.execute(update(RiskSettingsRow).where(RiskSettingsRow.mode == mode).values(row))


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """An empty Redis, shared by the whole test run. Tests clear it themselves."""
    _require_docker()
    container = (
        DockerContainer(REDIS_IMAGE)
        .with_exposed_ports(6379)
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    )
    with container:
        host = container.get_container_host_ip()
        yield f"redis://{host}:{container.get_exposed_port(6379)}/0"
