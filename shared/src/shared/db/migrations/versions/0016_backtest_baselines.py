"""An auto-switching backtest's baselines (P6-5c, D57): backtest_runs.baselines.

Each config the run switches between, run alone over the same candles, and buy-and-hold
(shared.backtest.BaselineResult), with only what their metrics need. NULL is a plain
run, or an auto-switching one stored before this, whose baselines were only printed.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("baselines", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_runs", "baselines")
