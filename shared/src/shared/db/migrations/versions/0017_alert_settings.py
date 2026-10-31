"""Create alert_settings: which alerts the alerts service sends to Telegram (P7-1, D59).

One row per event, seeded with each one's default: everything on except shrunk buys,
which an all-in bot past the per-coin limit makes on every buy. The Settings page turns
them on and off.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported, so this migration keeps meaning what it meant.
DEFAULTS = {
    "trade": True,
    "stop": True,
    "risk_block": True,
    "risk_resize": False,
    "breaker": True,
    "switch": True,
    "kill_switch": True,
    "error": True,
    "daily_summary": True,
}


def upgrade() -> None:
    settings = op.create_table(
        "alert_settings",
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "event IN ('trade', 'stop', 'risk_block', 'risk_resize', 'breaker', 'switch',"
            " 'kill_switch', 'error', 'daily_summary')",
            name="ck_alert_settings_event",
        ),
        sa.PrimaryKeyConstraint("event", name="pk_alert_settings"),
    )
    op.bulk_insert(settings, [{"event": e, "enabled": on} for e, on in DEFAULTS.items()])


def downgrade() -> None:
    op.drop_table("alert_settings")
