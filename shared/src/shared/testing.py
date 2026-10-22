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
from sqlalchemy import URL
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from shared.db.migrate import upgrade

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
