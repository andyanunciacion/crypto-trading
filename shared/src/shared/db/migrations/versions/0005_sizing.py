"""Add a sizing mode to bots and backtest runs (P4-1, D39).

Existing rows get all-in, which is how every bot and backtest sized its buys until now,
so nothing that ran before changes meaning.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("bots", "backtest_runs")
ALL_IN = '{"mode": "all_in"}'


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("sizing", postgresql.JSONB(), server_default=ALL_IN, nullable=False),
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "sizing")
