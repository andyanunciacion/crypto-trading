"""Create the candles hypertable.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The TimescaleDB image already loads the extension; this makes sure it's enabled here.
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table(
        "candles",
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("volume", sa.Numeric(), nullable=False),
        sa.CheckConstraint(
            "timeframe IN ('1m', '5m', '15m', '1h', '4h', '1d')", name="ck_candles_timeframe"
        ),
        sa.PrimaryKeyConstraint("exchange", "symbol", "timeframe", "ts", name="pk_candles"),
    )
    # A hypertable stores rows in time chunks (7 days each by default), so time-range
    # queries only touch the chunks they need. No extra index on ts alone: every query
    # filters by exchange/symbol/timeframe first, which the primary key already covers.
    op.execute(
        "SELECT create_hypertable('candles', by_range('ts'), create_default_indexes => false)"
    )


def downgrade() -> None:
    op.drop_table("candles")
