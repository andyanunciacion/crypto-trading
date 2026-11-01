"""A pytest plugin with fixtures for every package's tests. Test-only: never import it in app code.

pyproject.toml loads it with `-p shared.testing`. It lives here rather than in a root
conftest.py because mypy can't check two modules both named `conftest`.

`timescale_url` and `redis_url` each start a throwaway container (the same images as
compose.yaml) once per test run, via testcontainers (a dev dependency), and remove it
afterwards. They need Docker: without it, those tests are skipped locally, but fail in
CI so they can't quietly stop running.

`sentry_events` is a Sentry transport that keeps events instead of sending them (D62).
"""

import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
import sentry_sdk
import structlog
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sqlalchemy import URL, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from shared.alerts import to_alert
from shared.db.migrate import upgrade
from shared.db.models import BotRow, RiskSettingsRow
from shared.logs import mirrored_line, read_entry

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


def alert_of(captured: Mapping[str, Any], service: str = "engine") -> str | None:
    """The alert (shared/alerts.py, D59) a line `capture_logs()` caught is, read as the
    alerts service reads it from the logs stream; None if it isn't one.

    The tests that make the engine log the lines alerts are made from check them with
    this, so renaming an event or a field the alerts rely on fails there.
    """
    event_dict = {k: v for k, v in captured.items() if k != "log_level"}
    entry = read_entry(
        "0-0", mirrored_line({**event_dict, "level": captured["log_level"]}, service).encode()
    )
    alert = None if entry is None else to_alert(entry)
    return None if alert is None else alert.event


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


class SentryEvents(Transport):
    """A Sentry transport that keeps every event it's given (`init_errors(...,
    transport=)`), instead of sending it."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(dict(event))


def reset_sentry() -> None:
    """Back to no Sentry client, with empty scopes (breadcrumbs, tags)."""
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)  # the client that does nothing
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_global_scope().clear()


@pytest.fixture
def sentry_events() -> Iterator[SentryEvents]:
    """A fake Sentry transport; Sentry and structlog are reset before and after."""
    reset_sentry()
    yield SentryEvents()
    reset_sentry()
    structlog.reset_defaults()
