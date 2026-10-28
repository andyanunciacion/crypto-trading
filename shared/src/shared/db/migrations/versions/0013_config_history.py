"""Config history (P6-2, D52): strategy_configs.restored_from, and append-only enforced.

A rollback from the bot detail page adds a new row copying an earlier config's strategy
and params, with `restored_from` pointing at the row it copies. So each row is when a
config became active, and an order's `reason.strategy_config` always names one row.

The trigger refuses any UPDATE or DELETE of a row: a config, once made, never changes,
so the history (and every order's record of its config) can't be rewritten by mistake.
TRUNCATE doesn't fire row triggers, so tests can still empty the table.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("strategy_configs", sa.Column("restored_from", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_strategy_configs_restored_from_strategy_configs",
        "strategy_configs",
        "strategy_configs",
        ["restored_from"],
        ["id"],
    )
    op.execute(
        """
        CREATE FUNCTION strategy_configs_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_configs is append-only: add a row instead';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER strategy_configs_append_only
        BEFORE UPDATE OR DELETE ON strategy_configs
        FOR EACH ROW EXECUTE FUNCTION strategy_configs_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER strategy_configs_append_only ON strategy_configs")
    op.execute("DROP FUNCTION strategy_configs_append_only()")
    op.drop_constraint(
        "fk_strategy_configs_restored_from_strategy_configs", "strategy_configs", type_="foreignkey"
    )
    op.drop_column("strategy_configs", "restored_from")
