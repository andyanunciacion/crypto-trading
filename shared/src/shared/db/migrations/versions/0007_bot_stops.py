"""Add stops to bots, and keep each open position's exit levels (P4-2b, D40).

Existing bots get {} (no stops), which is how every bot ran until now. A position's
levels sit next to `stop` and `take_profit` (there since 0003), so a restarted engine
carries on with them: the stop-loss level (to tell which stop fired), the peak the
trailing stop follows, the ATR at entry, and the average entry they were placed from.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEVELS = ("stop_loss", "peak", "entry_atr", "levels_entry")


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("stops", postgresql.JSONB(), server_default="{}", nullable=False),
    )
    for column in LEVELS:
        op.add_column("positions", sa.Column(column, sa.Numeric(), nullable=True))


def downgrade() -> None:
    for column in reversed(LEVELS):
        op.drop_column("positions", column)
    op.drop_column("bots", "stops")
