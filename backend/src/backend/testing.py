"""A pytest plugin with the API's test fixtures. Test-only: never import it in app code.

pyproject.toml loads it with `-p backend.testing`, as it does shared.testing.

`api` is the real app (backend.app.create_app) on the throwaway test database and Redis,
with a clock the test sets, called in-process over HTTPS (so the Secure cookie is sent
back).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx2
import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app import create_app
from backend.auth.crypto import Secrets
from backend.settings import ApiSettings
from backend.users import create_user
from shared.db.session import make_engine

PASSWORD = "correct horse battery staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
T0 = datetime(2026, 10, 26, 12, 0, 5, tzinfo=UTC)  # 5 s into a 30 s TOTP step


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float = 30) -> None:
        self.now += timedelta(seconds=seconds)


def api_settings(redis_url: str = "redis://redis:6379/0") -> ApiSettings:
    """Settings for tests, with a fresh JWT secret and encryption key; nothing from .env.

    The feed sends a snapshot every 0.1 s and the kill switch waits 1 s, so tests are quick.
    """
    return ApiSettings(
        _env_file=None,
        postgres_user="test",
        postgres_password=SecretStr("test"),
        postgres_db="test",
        redis_url=redis_url,
        jwt_secret=SecretStr(Fernet.generate_key().decode()),  # 44 random characters
        encryption_key=SecretStr(Fernet.generate_key().decode()),
        feed_seconds=0.1,
        kill_reply_seconds=1,
    )


@dataclass
class Api:
    app: FastAPI
    client: httpx2.AsyncClient
    db: AsyncEngine
    redis: Redis
    clock: Clock
    settings: ApiSettings
    secrets: Secrets
    user_id: int = field(default=0)

    def code(self, at: datetime | None = None) -> str:
        """The test user's TOTP code at `at` (default: now)."""
        return pyotp.TOTP(TOTP_SECRET).at(at or self.clock.now)

    async def login(
        self, *, username: str = "andy", password: str = PASSWORD, code: str | None = None
    ) -> httpx2.Response:
        return await self.client.post(
            "/auth/login",
            json={"username": username, "password": password, "code": code or self.code()},
        )


@pytest.fixture
async def api(db_url: URL, redis_url: str) -> AsyncIterator[Api]:
    """The API with one user, `andy`, whose password is PASSWORD and secret TOTP_SECRET.

    Redis starts empty; `api.redis` is a client of the same one (bytes, like the engine's).
    """
    db = make_engine(db_url)
    async with db.begin() as conn:
        await conn.execute(text("TRUNCATE users RESTART IDENTITY"))
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    settings = api_settings(redis_url)
    secrets = Secrets(settings.encryption_key)
    user_id = await create_user(db, secrets, "andy", PASSWORD, TOTP_SECRET, last_step=None)
    clock = Clock()
    app = create_app(settings, database_url=db_url, clock=clock)
    # ASGITransport doesn't run the app's lifespan (which opens its database): this does.
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://testserver"
        ) as client,
    ):
        yield Api(app, client, db, redis, clock, settings, secrets, user_id)
    await redis.aclose()
    await db.dispose()
