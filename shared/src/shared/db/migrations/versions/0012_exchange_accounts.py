"""Create exchange_accounts: the exchange API keys the Settings page stores (P5-7, D50).

The key and the secret are each encrypted with Fernet under `ENCRYPTION_KEY` (D12),
which lives only in the server's environment, so a database dump doesn't reveal them.
`key_hint` is the API key's last 4 characters, so the page can tell keys apart without
ever sending one back. Nothing trades with them until P9's LiveBroker.

MEXC has no sandbox, so a MEXC account with `sandbox` set is refused (as the adapter
refuses one, D22); Binance's is its spot testnet (D23).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exchange_accounts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("api_key_enc", sa.Text(), nullable=False),
        sa.Column("api_secret_enc", sa.Text(), nullable=False),
        sa.Column("key_hint", sa.Text(), nullable=False),
        sa.Column("sandbox", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("exchange IN ('mexc', 'binance')", name="ck_exchange_accounts_exchange"),
        sa.CheckConstraint(
            "NOT (exchange = 'mexc' AND sandbox)", name="ck_exchange_accounts_sandbox"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exchange_accounts"),
        sa.UniqueConstraint("label", name="uq_exchange_accounts_label"),
    )


def downgrade() -> None:
    op.drop_table("exchange_accounts")
