"""Create the orders, fills, positions and paper_balances tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # bot_id has no foreign key yet: the bots table comes with the bot runner (P3-3).
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("exchange_order_id", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("filled", sa.Numeric(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("reason", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("mode IN ('paper', 'live')", name="ck_orders_mode"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_orders_side"),
        sa.CheckConstraint("type IN ('limit')", name="ck_orders_type"),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'canceled', 'rejected')", name="ck_orders_status"
        ),
        sa.CheckConstraint("price > 0 AND amount > 0", name="ck_orders_positive"),
        sa.CheckConstraint("filled >= 0 AND filled <= amount", name="ck_orders_filled"),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        # The database itself refuses a second order with the same client order ID (D18).
        sa.UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
    )
    op.create_index(
        "ix_orders_open",
        "orders",
        ["exchange", "symbol"],
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "fills",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("fee", sa.Numeric(), nullable=False),
        sa.Column("fee_currency", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_fills_order_id_orders"),
        sa.PrimaryKeyConstraint("id", name="pk_fills"),
    )
    op.create_index("ix_fills_order_id", "fills", ["order_id"])

    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("avg_entry", sa.Numeric(), nullable=False),
        sa.Column("fees_paid", sa.Numeric(), nullable=False),
        sa.Column("stop", sa.Numeric(), nullable=True),
        sa.Column("take_profit", sa.Numeric(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(), nullable=False),
        sa.CheckConstraint("mode IN ('paper', 'live')", name="ck_positions_mode"),
        sa.CheckConstraint("side IN ('long')", name="ck_positions_side"),
        sa.CheckConstraint("amount >= 0", name="ck_positions_amount"),
        sa.PrimaryKeyConstraint("id", name="pk_positions"),
    )
    # A partial unique index: closed positions pile up as history, but at most one per
    # bot and symbol can be open at a time.
    op.create_index(
        "uq_positions_open",
        "positions",
        ["bot_id", "symbol"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "paper_balances",
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("free", sa.Numeric(), nullable=False),
        sa.Column("used", sa.Numeric(), nullable=False),
        sa.CheckConstraint("free >= 0 AND used >= 0", name="ck_paper_balances_not_negative"),
        sa.PrimaryKeyConstraint("bot_id", "currency", name="pk_paper_balances"),
    )


def downgrade() -> None:
    op.drop_table("paper_balances")
    op.drop_index("uq_positions_open", table_name="positions")
    op.drop_table("positions")
    op.drop_index("ix_fills_order_id", table_name="fills")
    op.drop_table("fills")
    op.drop_index("ix_orders_open", table_name="orders")
    op.drop_table("orders")
