"""The migrations against a real TimescaleDB (see the root conftest.py).

Plain (sync) tests on purpose: Alembic runs its own event loop via asyncio.run(),
which can't start inside a test that's already running on one.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import URL, Connection, text

from shared.db.migrate import downgrade, upgrade
from shared.db.models import Base
from shared.db.session import make_engine


def run_on(url: URL, work: Callable[[Connection], Any]) -> Any:
    """Run sync `work` on a connection to `url`, in a fresh event loop."""

    async def main() -> Any:
        engine = make_engine(url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(work)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def hypertables(conn: Connection) -> list[str]:
    rows = conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables"))
    return [row[0] for row in rows]


def schema_drift(conn: Connection) -> list[Any]:
    diffs: list[Any] = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    return diffs


def test_candles_is_a_hypertable(db_url: URL) -> None:
    assert run_on(db_url, hypertables) == ["candles"]


def test_migrations_match_the_models(db_url: URL) -> None:
    # Alembic's autogenerate diff: anything here means models.py and the migrations disagree.
    assert run_on(db_url, schema_drift) == []


def test_downgrade_and_upgrade_again(db_url: URL) -> None:
    downgrade(db_url, "base")
    assert run_on(db_url, hypertables) == []

    upgrade(db_url)  # leaves the database migrated for the other tests
    assert run_on(db_url, hypertables) == ["candles"]
