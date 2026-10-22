"""The async database engine (a connection pool) for the API and the engine."""

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(url: URL) -> AsyncEngine:
    """One per process; dispose() it on shutdown. Pass `Settings.database_url`."""
    # pool_pre_ping checks a pooled connection before use, so a Postgres restart
    # doesn't surface as an error on the next query.
    return create_async_engine(url, pool_pre_ping=True)
