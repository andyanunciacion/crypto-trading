"""Add why and when a bot was stopped (P4-5, D43).

The kill switch stops every bot with `status = 'stopped'`, the reason (which command,
from where) and the time, so a stopped bot shows what stopped it. NULL for a bot
stopped by hand.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bots", sa.Column("stopped_reason", sa.Text(), nullable=True))
    op.add_column("bots", sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bots", "stopped_at")
    op.drop_column("bots", "stopped_reason")
