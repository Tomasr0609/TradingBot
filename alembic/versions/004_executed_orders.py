"""Add positions and executed_orders

Revision ID: 004
Revises: 003
Create Date: 2026-08-30
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("size", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("stop_loss", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_symbol", "positions", ["symbol"])
    op.create_table(
        "executed_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("signal_type", sa.String(length=10), nullable=False),
        sa.Column("signal_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("strategy_name", sa.String(length=50), nullable=False),
        sa.Column("regime", sa.String(length=20), nullable=False),
        sa.Column("atr", sa.Numeric(precision=20, scale=8)),
        sa.Column("risk_decision", sa.String(length=20), nullable=False),
        sa.Column("risk_rule", sa.String(length=30), nullable=False),
        sa.Column("risk_reason", sa.Text(), nullable=False),
        sa.Column("approved_size", sa.Numeric(precision=20, scale=8)),
        sa.Column("stop_loss_price", sa.Numeric(precision=20, scale=8)),
        sa.Column("trailing_stop", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("order_id", sa.String(length=100)),
        sa.Column("order_type", sa.String(length=20)),
        sa.Column("requested_size", sa.Numeric(precision=20, scale=8)),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("executed_price", sa.Numeric(precision=20, scale=8)),
        sa.Column("executed_size", sa.Numeric(precision=20, scale=8)),
        sa.Column("fee", sa.Numeric(precision=20, scale=8)),
        sa.Column("raw_response", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("is_testnet", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executed_orders_created_at", "executed_orders", ["created_at"])
    op.create_index("ix_executed_orders_symbol", "executed_orders", ["symbol"])

def downgrade() -> None:
    op.drop_index("ix_executed_orders_symbol", table_name="executed_orders")
    op.drop_index("ix_executed_orders_created_at", table_name="executed_orders")
    op.drop_table("executed_orders")
    op.drop_index("ix_positions_symbol", table_name="positions")
    op.drop_table("positions")
