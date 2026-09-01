"""Add total_exposure to daily_stats

Revision ID: 003
Revises: 002
Create Date: 2026-08-29
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("daily_stats", sa.Column("total_exposure", sa.Numeric(precision=20, scale=8), server_default="0", nullable=False))

def downgrade() -> None:
    op.drop_column("daily_stats", "total_exposure")
