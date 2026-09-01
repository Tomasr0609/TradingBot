"""Initial migration - create klines table

Revision ID: 001
Revises: 
Create Date: 2026-08-26

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "klines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("timeframe", sa.String(length=10), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("high_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("low_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("close_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("volume", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("quote_volume", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("trades_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "taker_buy_base_volume", sa.Numeric(precision=20, scale=8), nullable=False
        ),
        sa.Column(
            "taker_buy_quote_volume", sa.Numeric(precision=20, scale=8), nullable=False
        ),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol", "timeframe", "open_time", name="uq_kline_symbol_tf_opentime"
        ),
    )
    op.create_index("ix_kline_symbol_tf_closed", "klines", ["symbol", "timeframe", "is_closed"])
    op.create_index("ix_kline_symbol_tf_opentime", "klines", ["symbol", "timeframe", "open_time"])


def downgrade() -> None:
    op.drop_index("ix_kline_symbol_tf_opentime", table_name="klines")
    op.drop_index("ix_kline_symbol_tf_closed", table_name="klines")
    op.drop_table("klines")