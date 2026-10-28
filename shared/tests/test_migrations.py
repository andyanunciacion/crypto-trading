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


def test_0014_records_the_earlier_switches_from_the_config_history(db_url: URL) -> None:
    # Two bots: one switched by hand and then rolled back, one never switched.
    downgrade(db_url, "0013")
    try:

        def history(conn: Connection) -> None:
            # Other tests' rows may still be here; the ids below must start at 1.
            conn.execute(text("TRUNCATE bots, strategy_configs RESTART IDENTITY CASCADE"))
            conn.execute(
                text(
                    "INSERT INTO bots (name, exchange, symbol, timeframe)"
                    " VALUES ('a', 'mexc', 'BTC/USDT', '1h'), ('b', 'mexc', 'ETH/USDT', '1h')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO strategy_configs (bot_id, strategy, version, params, created_by,"
                    " restored_from) VALUES"
                    " (1, 'ema_crossover', 1, '{}', 'cli', NULL),"
                    " (2, 'ema_crossover', 1, '{}', 'cli', NULL),"
                    " (1, 'rsi_mean_reversion', 1, '{}', 'dashboard', NULL),"
                    " (1, 'ema_crossover', 1, '{}', 'dashboard', 1)"
                )
            )
            conn.commit()

        run_on(db_url, history)
        upgrade(db_url)

        def switches(conn: Connection) -> list[tuple[Any, ...]]:
            rows = conn.execute(
                text(
                    "SELECT s.bot_id, s.from_config, s.to_config, s.trigger, s.policy, s.source,"
                    " s.ts = c.created_at FROM strategy_switches s"
                    " JOIN strategy_configs c ON c.id = s.to_config ORDER BY s.id"
                )
            )
            return [tuple(row) for row in rows]

        assert run_on(db_url, switches) == [
            (1, 1, 3, "manual", None, "dashboard", True),
            (1, 3, 4, "rollback", None, "dashboard", True),
        ]
    finally:
        upgrade(db_url)

        def empty(conn: Connection) -> None:
            conn.execute(text("TRUNCATE bots, strategy_configs, strategy_switches CASCADE"))
            conn.commit()

        run_on(db_url, empty)


def test_downgrade_and_upgrade_again(db_url: URL) -> None:
    downgrade(db_url, "base")
    assert run_on(db_url, hypertables) == []

    upgrade(db_url)  # leaves the database migrated for the other tests
    assert run_on(db_url, hypertables) == ["candles"]
