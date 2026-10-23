"""Create the bots and strategy_configs tables, and point the trading tables' bot_id at bots.

Adding those foreign keys fails if orders, positions or paper_balances already hold rows
for a bot that doesn't exist. Until P3-3 only tests wrote to them, so a real database
has none.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRADING_TABLES = ("orders", "positions", "paper_balances")


def upgrade() -> None:
    op.create_table(
        "bots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), server_default="paper", nullable=False),
        sa.Column("status", sa.Text(), server_default="stopped", nullable=False),
        sa.Column("active_strategy_config_id", sa.BigInteger(), nullable=True),
        sa.Column("switch_policy", sa.Text(), server_default="keep", nullable=False),
        sa.CheckConstraint(
            "timeframe IN ('1m', '5m', '15m', '1h', '4h', '1d')", name="ck_bots_timeframe"
        ),
        sa.CheckConstraint("mode IN ('paper', 'live')", name="ck_bots_mode"),
        sa.CheckConstraint("status IN ('running', 'paused', 'stopped')", name="ck_bots_status"),
        sa.CheckConstraint(
            "switch_policy IN ('keep', 'close_first', 'hand_over')", name="ck_bots_switch_policy"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bots"),
        sa.UniqueConstraint("name", name="uq_bots_name"),
    )

    op.create_table(
        "strategy_configs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], name="fk_strategy_configs_bot_id_bots"),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_configs"),
    )
    op.create_index("ix_strategy_configs_bot_id", "strategy_configs", ["bot_id"])

    # Now that both tables exist, the bot's pointer to the config it runs.
    op.create_foreign_key(
        "fk_bots_active_strategy_config_id_strategy_configs",
        "bots",
        "strategy_configs",
        ["active_strategy_config_id"],
        ["id"],
    )

    # The trading tables waited for this table (0003): every row now names a real bot.
    for table in TRADING_TABLES:
        op.create_foreign_key(f"fk_{table}_bot_id_bots", table, "bots", ["bot_id"], ["id"])


def downgrade() -> None:
    for table in TRADING_TABLES:
        op.drop_constraint(f"fk_{table}_bot_id_bots", table, type_="foreignkey")
    op.drop_constraint(
        "fk_bots_active_strategy_config_id_strategy_configs", "bots", type_="foreignkey"
    )
    op.drop_index("ix_strategy_configs_bot_id", table_name="strategy_configs")
    op.drop_table("strategy_configs")
    op.drop_table("bots")
