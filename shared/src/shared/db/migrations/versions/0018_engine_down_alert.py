"""Add the `engine_down` alert to alert_settings (P7-3, D61), on by default.

The alerts service sends it when the engine's heartbeat stops for a minute, and again
when it's back. A toggle like the others, so it can be muted while working on the engine.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported, so this migration keeps meaning what it meant.
DEFAULTS = {"engine_down": True}

BEFORE = (
    "'trade', 'stop', 'risk_block', 'risk_resize', 'breaker', 'switch', 'kill_switch',"
    " 'error', 'daily_summary'"
)
AFTER = f"{BEFORE}, 'engine_down'"

# The name the constraint got in 0017 (the naming convention adds `ck_<table>_`).
CHECK = "ck_alert_settings_event"


def upgrade() -> None:
    op.drop_constraint(CHECK, "alert_settings", type_="check")
    op.create_check_constraint(CHECK, "alert_settings", f"event IN ({AFTER})")
    settings = sa.table("alert_settings", sa.column("event", sa.Text), sa.column("enabled"))
    op.bulk_insert(settings, [{"event": e, "enabled": on} for e, on in DEFAULTS.items()])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM alert_settings WHERE event = 'engine_down'"))
    op.drop_constraint(CHECK, "alert_settings", type_="check")
    op.create_check_constraint(CHECK, "alert_settings", f"event IN ({BEFORE})")
