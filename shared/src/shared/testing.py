"""A pytest plugin with fixtures for every package's tests. Test-only: never import it in app code.

pyproject.toml loads it with `-p shared.testing`. It lives here rather than in a root
conftest.py because mypy can't check two modules both named `conftest`.

`timescale_url` starts a throwaway TimescaleDB container (the same image as compose.yaml)
once per test run, via testcontainers (a dev dependency), and removes it afterwards. It
needs Docker: without it, database tests are skipped locally, but fail in CI so they
can't quietly stop running.
"""

import os
import shutil
import subprocess
from collections.abc import Iterator

import pytest
from sqlalchemy import URL
from testcontainers.community.postgres import PostgresContainer

from shared.db.migrate import upgrade

TIMESCALE_IMAGE = "timescale/timescaledb:2.31.0-pg16"  # keep in step with compose.yaml


def _docker_running() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def timescale_url() -> Iterator[URL]:
    """An empty TimescaleDB database, shared by the whole test run."""
    if not _docker_running():
        reason = "database tests need Docker; start Docker Desktop"
        if os.environ.get("CI"):
            pytest.fail(reason)
        pytest.skip(reason)
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
