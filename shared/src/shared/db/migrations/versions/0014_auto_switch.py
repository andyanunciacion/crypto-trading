"""Auto-switching (P6-4, D54): bots.auto_switch, and strategy_switches.

`bots.auto_switch` holds a bot's regime -> strategy mapping, its minimum hold and its
switch policy (shared.regime.AutoSwitchSettings); NULL is off.

`strategy_switches` records every switch of a bot's strategy config: from the Bots
page (`manual`), a rollback (`rollback`) or the regime (`regime`, with the regime, the
candle that confirmed it and the detector's reading). The chart marks them and the bot
page lists them. Like `strategy_configs`, it's append-only, enforced by a trigger.

The switches made before this (P6-1, P6-2) are recorded from the config history: each
config after a bot's first was a switch to it, at the time it was made.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bots", sa.Column("auto_switch", JSONB(), nullable=True))
    op.create_table(
        "strategy_switches",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("from_config", sa.BigInteger(), nullable=True),
        sa.Column("to_config", sa.BigInteger(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("policy", sa.Text(), nullable=True),
        sa.Column("regime", sa.Text(), nullable=True),
        sa.Column("candle_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reading", JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "trigger IN ('manual', 'rollback', 'regime')", name="ck_strategy_switches_trigger"
        ),
        sa.CheckConstraint(
            "policy IN ('keep', 'close_first', 'hand_over')", name="ck_strategy_switches_policy"
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], name="fk_strategy_switches_bot_id_bots"),
        sa.ForeignKeyConstraint(
            ["from_config"],
            ["strategy_configs.id"],
            name="fk_strategy_switches_from_config_strategy_configs",
        ),
        sa.ForeignKeyConstraint(
            ["to_config"],
            ["strategy_configs.id"],
            name="fk_strategy_switches_to_config_strategy_configs",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_switches"),
    )
    op.create_index("ix_strategy_switches_bot_id", "strategy_switches", ["bot_id"])
    op.execute(
        """
        INSERT INTO strategy_switches (ts, bot_id, from_config, to_config, trigger, reason, source)
        SELECT created_at, bot_id, previous, id,
               CASE WHEN restored_from IS NULL THEN 'manual' ELSE 'rollback' END,
               'recorded from the config history (made before switches were logged)',
               created_by
        FROM (
            SELECT *, lag(id) OVER (PARTITION BY bot_id ORDER BY id) AS previous
            FROM strategy_configs
        ) AS configs
        WHERE previous IS NOT NULL
        ORDER BY id
        """
    )
    op.execute(
        """
        CREATE FUNCTION strategy_switches_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_switches is append-only: add a row instead';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER strategy_switches_append_only
        BEFORE UPDATE OR DELETE ON strategy_switches
        FOR EACH ROW EXECUTE FUNCTION strategy_switches_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER strategy_switches_append_only ON strategy_switches")
    op.execute("DROP FUNCTION strategy_switches_append_only()")
    op.drop_index("ix_strategy_switches_bot_id", table_name="strategy_switches")
    op.drop_table("strategy_switches")
    op.drop_column("bots", "auto_switch")
