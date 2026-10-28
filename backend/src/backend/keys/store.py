"""The `exchange_accounts` rows (D50): exchange API keys, encrypted at rest (D12).

The key and secret are encrypted before they reach the database and are never read back
out by anything here: `AccountView` has no field for them, so no route can return one.
Only P9's LiveBroker will decrypt them.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from backend.auth.crypto import Secrets
from shared.db.models import ExchangeAccountRow

Exchange = Literal["mexc", "binance"]

HINT_LENGTH = 4


class AccountView(BaseModel):
    """What the browser may see of an account: never the key or the secret."""

    id: int
    exchange: Exchange
    label: str
    sandbox: bool
    key_hint: str  # the API key's last 4 characters
    created_at: datetime
    updated_at: datetime


_VIEW = (
    ExchangeAccountRow.id,
    ExchangeAccountRow.exchange,
    ExchangeAccountRow.label,
    ExchangeAccountRow.sandbox,
    ExchangeAccountRow.key_hint,
    ExchangeAccountRow.created_at,
    ExchangeAccountRow.updated_at,
)


def hint(api_key: str) -> str:
    return api_key[-HINT_LENGTH:]


async def read_all(db: AsyncEngine) -> list[AccountView]:
    async with db.connect() as conn:
        rows = await conn.execute(select(*_VIEW).order_by(ExchangeAccountRow.id))
        return [AccountView.model_validate(row._asdict()) for row in rows]


async def label_taken(conn: AsyncConnection, label: str) -> bool:
    query = select(ExchangeAccountRow.id).where(ExchangeAccountRow.label == label)
    return (await conn.scalar(query)) is not None


async def exists(conn: AsyncConnection, account_id: int) -> bool:
    query = select(ExchangeAccountRow.id).where(ExchangeAccountRow.id == account_id)
    return (await conn.scalar(query)) is not None


async def add(
    conn: AsyncConnection,
    secrets: Secrets,
    *,
    exchange: Exchange,
    label: str,
    api_key: str,
    api_secret: str,
    sandbox: bool,
    now: datetime,
) -> AccountView:
    row = (
        await conn.execute(
            insert(ExchangeAccountRow)
            .values(
                exchange=exchange,
                label=label,
                api_key_enc=secrets.encrypt(api_key),
                api_secret_enc=secrets.encrypt(api_secret),
                key_hint=hint(api_key),
                sandbox=sandbox,
                created_at=now,
                updated_at=now,
            )
            .returning(*_VIEW)
        )
    ).one()
    return AccountView.model_validate(row._asdict())


async def replace_keys(
    conn: AsyncConnection,
    secrets: Secrets,
    account_id: int,
    *,
    api_key: str,
    api_secret: str,
    now: datetime,
) -> AccountView:
    row = (
        await conn.execute(
            update(ExchangeAccountRow)
            .where(ExchangeAccountRow.id == account_id)
            .values(
                api_key_enc=secrets.encrypt(api_key),
                api_secret_enc=secrets.encrypt(api_secret),
                key_hint=hint(api_key),
                updated_at=now,
            )
            .returning(*_VIEW)
        )
    ).one()
    return AccountView.model_validate(row._asdict())


async def remove(db: AsyncEngine, account_id: int) -> AccountView | None:
    """Delete the account; None if there's none with that id."""
    async with db.begin() as conn:
        row = (
            await conn.execute(
                delete(ExchangeAccountRow)
                .where(ExchangeAccountRow.id == account_id)
                .returning(*_VIEW)
            )
        ).one_or_none()
    return None if row is None else AccountView.model_validate(row._asdict())
