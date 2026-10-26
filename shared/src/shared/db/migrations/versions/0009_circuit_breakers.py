"""Add the circuit breakers' settings, and why a bot is paused (P4-4, D42).

`risk_settings` gets the breakers, seeded in both modes with ARCHITECTURE §6's values:
no buys on a price older than 2 candles, a bot paused after 3 failed orders in 10
minutes, and no buys of a coin whose price swung 3% or more within 5 minutes. NULL
turns one off; each pair is both set or both NULL.

A bot the error breaker pauses gets `status = 'paused'` with the reason and the time,
so it stays paused across a restart until it's set running again.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-26
"""

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STARTING_BREAKERS = {
    "stale_after_candles": 2,
    "max_failed_orders": 3,
    "failed_orders_minutes": 10,
    "spike_pct": Decimal(3),
    "spike_minutes": 5,
}
BREAKERS_CHECK = (
    "stale_after_candles >= 1"
    " AND max_failed_orders >= 1 AND failed_orders_minutes >= 1"
    " AND (max_failed_orders IS NULL) = (failed_orders_minutes IS NULL)"
    " AND spike_pct > 0 AND spike_minutes >= 1 AND spike_minutes <= 60"
    " AND (spike_pct IS NULL) = (spike_minutes IS NULL)"
)


def upgrade() -> None:
    op.add_column("risk_settings", sa.Column("stale_after_candles", sa.Integer(), nullable=True))
    op.add_column("risk_settings", sa.Column("max_failed_orders", sa.Integer(), nullable=True))
    op.add_column("risk_settings", sa.Column("failed_orders_minutes", sa.Integer(), nullable=True))
    op.add_column("risk_settings", sa.Column("spike_pct", sa.Numeric(), nullable=True))
    op.add_column("risk_settings", sa.Column("spike_minutes", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_risk_settings_breakers", "risk_settings", BREAKERS_CHECK)
    settings = sa.table(
        "risk_settings",
        sa.column("stale_after_candles", sa.Integer()),
        sa.column("max_failed_orders", sa.Integer()),
        sa.column("failed_orders_minutes", sa.Integer()),
        sa.column("spike_pct", sa.Numeric()),
        sa.column("spike_minutes", sa.Integer()),
    )
    op.execute(settings.update().values(STARTING_BREAKERS))

    op.add_column("bots", sa.Column("paused_reason", sa.Text(), nullable=True))
    op.add_column("bots", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bots", "paused_at")
    op.drop_column("bots", "paused_reason")
    op.drop_constraint("ck_risk_settings_breakers", "risk_settings", type_="check")
    for column in reversed(STARTING_BREAKERS):
        op.drop_column("risk_settings", column)
