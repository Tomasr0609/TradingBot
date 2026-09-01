"""Add risk management tables

Revision ID: 002
Revises: 001
Create Date: 2026-08-26

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "risk_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("signal_type", sa.String(length=10), nullable=False),
        sa.Column("signal_price", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("strategy_name", sa.String(length=50), nullable=False),
        sa.Column("regime", sa.String(length=20), nullable=False),
        sa.Column("decision", sa.Enum("approved", "reduced", "rejected", name="riskdecision"), nullable=False),
        sa.Column("triggered_rule", sa.Enum("daily_loss_limit", "position_sizing", "stop_loss_required", "total_exposure", "circuit_breaker", "max_drawdown", "kill_switch", "data_integrity", "connection_error", name="riskrule"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("original_size", sa.Numeric(precision=20, scale=8)),
        sa.Column("approved_size", sa.Numeric(precision=20, scale=8)),
        sa.Column("risk_pct", sa.Numeric(precision=10, scale=6)),
        sa.Column("stop_loss_price", sa.Numeric(precision=20, scale=8)),
        sa.Column("stop_loss_type", sa.String(length=20)),
        sa.Column("account_equity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(precision=20, scale=8), server_default=sa.text("0")),
        sa.Column("total_exposure", sa.Numeric(precision=20, scale=8), server_default=sa.text("0")),
        sa.Column("current_drawdown", sa.Numeric(precision=10, scale=6), server_default=sa.text("0")),
        sa.Column("metadata_json", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risk_log_timestamp_decision", "risk_logs", ["timestamp", "decision"])
    op.create_index("ix_risk_log_symbol_timestamp", "risk_logs", ["symbol", "timestamp"])
    op.create_index("ix_risk_log_decision", "risk_logs", ["decision"])
    op.create_index("ix_risk_log_triggered_rule", "risk_logs", ["triggered_rule"])

    op.create_table(
        "daily_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("starting_equity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("current_equity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(precision=20, scale=8), server_default=sa.text("0")),
        sa.Column("daily_pnl_pct", sa.Numeric(precision=10, scale=6), server_default=sa.text("0")),
        sa.Column("peak_equity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("max_drawdown_pct", sa.Numeric(precision=10, scale=6), server_default=sa.text("0")),
        sa.Column("trades_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column("winning_trades", sa.Integer(), server_default=sa.text("0")),
        sa.Column("losing_trades", sa.Integer(), server_default=sa.text("0")),
        sa.Column("daily_loss_limit_pct", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("is_trading_halted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("halt_reason", sa.String(length=100)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", name="uq_daily_stats_date"),
    )
    op.create_index("ix_daily_stats_date", "daily_stats", ["date"])

    op.create_table(
        "kill_switch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("activated_by", sa.String(length=100)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.Text()),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("kill_switch")
    op.drop_index("ix_daily_stats_date", table_name="daily_stats")
    op.drop_table("daily_stats")
    op.drop_index("ix_risk_log_triggered_rule", table_name="risk_logs")
    op.drop_index("ix_risk_log_decision", table_name="risk_logs")
    op.drop_index("ix_risk_log_symbol_timestamp", table_name="risk_logs")
    op.drop_index("ix_risk_log_timestamp_decision", table_name="risk_logs")
    op.drop_table("risk_logs")