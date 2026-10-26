"""Create users: who can sign in to the dashboard (P5-1, D44).

One user, made by `python -m backend.users create`: an argon2id password hash and a
TOTP secret encrypted with Fernet (D12). It also keeps the last TOTP step used (so a
code can't be replayed), a session version (logout bumps it, ending every session),
and the failed logins in a row with the lockout they lead to.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret_enc", sa.Text(), nullable=False),
        sa.Column("totp_last_step", sa.BigInteger(), nullable=True),
        sa.Column("session_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("failed_logins", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("failed_logins >= 0 AND session_version >= 1", name="ck_users_counters"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )


def downgrade() -> None:
    op.drop_table("users")
