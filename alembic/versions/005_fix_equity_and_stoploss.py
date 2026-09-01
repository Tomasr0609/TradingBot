"""Fix equity continuity and stop loss protection

Revision ID: 005
Revises: 004
Create Date: 2026-08-30
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "global_risk_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("peak_equity", sa.Numeric(precision=20, scale=8), nullable=False, server_default="10000"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
    )
    # Seed initial row - SQLite compatible
    op.execute("INSERT OR IGNORE INTO global_risk_state (id, peak_equity) VALUES (1, 10000)")
    op.add_column("executed_orders", sa.Column("stop_loss_order_id", sa.String(length=100), nullable=True))
    op.add_column("executed_orders", sa.Column("has_protection", sa.Boolean(), server_default=sa.text("0"), nullable=False))

def downgrade() -> None:
    op.drop_column("executed_orders", "has_protection")
    op.drop_column("executed_orders", "stop_loss_order_id")
    op.drop_table("global_risk_state")
