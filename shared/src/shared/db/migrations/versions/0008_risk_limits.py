"""Create risk_settings and risk_events: the account's limits, and what they blocked (P4-3, D41).

Both modes start with ARCHITECTURE §6's limits: 3 open positions, 25% of equity per
coin, a 3% daily loss and a 10% drawdown. NULL turns a limit off.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26
"""

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STARTING_LIMITS = {
    "max_open_positions": 3,
    "max_coin_exposure_pct": Decimal(25),
    "max_daily_loss_pct": Decimal(3),
    "max_drawdown_pct": Decimal(10),
}


def upgrade() -> None:
    settings = op.create_table(
        "risk_settings",
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("max_open_positions", sa.Integer(), nullable=True),
        sa.Column("max_coin_exposure_pct", sa.Numeric(), nullable=True),
        sa.Column("max_daily_loss_pct", sa.Numeric(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("mode IN ('paper', 'live')", name="ck_risk_settings_mode"),
        sa.CheckConstraint(
            "max_open_positions >= 1"
            " AND max_coin_exposure_pct > 0 AND max_coin_exposure_pct <= 100"
            " AND max_daily_loss_pct > 0 AND max_daily_loss_pct <= 100"
            " AND max_drawdown_pct > 0 AND max_drawdown_pct <= 100",
            name="ck_risk_settings_limits",
        ),
        sa.PrimaryKeyConstraint("mode", name="pk_risk_settings"),
    )
    op.bulk_insert(settings, [{"mode": mode, **STARTING_LIMITS} for mode in ("paper", "live")])

    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("mode IN ('paper', 'live')", name="ck_risk_events_mode"),
        sa.CheckConstraint(
            "decision IN ('allowed', 'blocked', 'resized')", name="ck_risk_events_decision"
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], name="fk_risk_events_bot_id_bots"),
        sa.PrimaryKeyConstraint("id", name="pk_risk_events"),
    )
    op.create_index("ix_risk_events_mode_rule_ts", "risk_events", ["mode", "rule", "ts"])


def downgrade() -> None:
    op.drop_index("ix_risk_events_mode_rule_ts", table_name="risk_events")
    op.drop_table("risk_events")
    op.drop_table("risk_settings")
