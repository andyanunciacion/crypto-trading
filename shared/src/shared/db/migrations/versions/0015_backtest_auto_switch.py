"""Auto-switching backtests (P6-5, D55): backtest_runs.auto_switch and the detector's version.

`auto_switch` is the run's regime -> strategy mapping, minimum hold and switch policy
(shared.regime.AutoSwitchSettings); NULL is a plain run, as every run before this was.
`regime_detector_version` is the detector's version it ran with, so a run whose
detector has changed since isn't "reproduced" by different rules.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("auto_switch", JSONB(), nullable=True))
    op.add_column(
        "backtest_runs", sa.Column("regime_detector_version", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("backtest_runs", "regime_detector_version")
    op.drop_column("backtest_runs", "auto_switch")
