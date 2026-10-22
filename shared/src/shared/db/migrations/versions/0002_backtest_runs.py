"""Create the backtest_runs table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain table: a few rows per backtest run, nothing a hypertable would help with.
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("warmup_candles", sa.Integer(), nullable=False),
        sa.Column("initial_cash", sa.Numeric(), nullable=False),
        sa.Column("fee_rate", sa.Numeric(), nullable=False),
        sa.Column("slippage_rate", sa.Numeric(), nullable=False),
        sa.Column("market_rules", postgresql.JSONB(), nullable=False),
        sa.Column("candle_count", sa.Integer(), nullable=False),
        sa.Column("candles_sha256", sa.Text(), nullable=False),
        sa.Column("backtester_version", sa.Integer(), nullable=False),
        sa.Column("final_equity", sa.Numeric(), nullable=False),
        sa.Column("total_fees", sa.Numeric(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "timeframe IN ('1m', '5m', '15m', '1h', '4h', '1d')",
            name="ck_backtest_runs_timeframe",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_runs"),
    )


def downgrade() -> None:
    op.drop_table("backtest_runs")
