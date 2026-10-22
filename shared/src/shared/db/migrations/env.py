"""Alembic's entry point: runs the migrations over an async (asyncpg) connection.

shared.db.migrate passes the database URL in `config.attributes`; when it's missing
(e.g. the `alembic` CLI), the URL comes from the app settings.
"""

import asyncio

from alembic import context
from sqlalchemy import URL, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from shared.config import load_settings
from shared.db.models import Base

config = context.config


def _url() -> URL:
    url = config.attributes.get("url")
    if isinstance(url, URL):
        return url
    return load_settings().database_url


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    # NullPool: one connection for the migration run, closed straight after.
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            # Alembic's API is synchronous; run_sync hands it a sync view of the connection.
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations aren't supported; run against a database")
asyncio.run(_run_async())
