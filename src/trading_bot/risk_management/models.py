"""Risk Management Models - Database models for risk tracking."""

from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum
from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.storage.models import Base


class RiskDecision(PyEnum):
    """Risk module decision on a signal."""
    APPROVED = "approved"
    REDUCED = "reduced"
    REJECTED = "rejected"


class RiskRule(PyEnum):
    """Which risk rule triggered the decision."""
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    POSITION_SIZING = "position_sizing"
    STOP_LOSS_REQUIRED = "stop_loss_required"
    TOTAL_EXPOSURE = "total_exposure"
    CIRCUIT_BREAKER = "circuit_breaker"
    MAX_DRAWDOWN = "max_drawdown"
    KILL_SWITCH = "kill_switch"
    DATA_INTEGRITY = "data_integrity"
    CONNECTION_ERROR = "connection_error"
    SENTIMENT_VETO = "sentiment_veto"
    SENTIMENT_REDUCE = "sentiment_reduce"
    MACRO_PAUSE = "macro_pause"


class RiskLog(Base):
    """Audit log for every risk decision."""

    __tablename__ = "risk_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False, index=True
    )

    # Signal info
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY/SELL
    signal_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(50), nullable=False)
    regime: Mapped[str] = mapped_column(String(20), nullable=False)

    # Risk decision
    decision: Mapped[RiskDecision] = mapped_column(
        Enum(RiskDecision), nullable=False, index=True
    )
    triggered_rule: Mapped[RiskRule] = mapped_column(
        Enum(RiskRule), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # Position sizing
    original_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    approved_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    risk_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))  # % of capital at risk

    # Stop loss
    stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_loss_type: Mapped[str | None] = mapped_column(String(20))  # atr, fixed, trailing

    # Account state at decision time
    account_equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    daily_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    total_exposure: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    current_drawdown: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))

    # Additional context
    metadata_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_risk_log_timestamp_decision", "timestamp", "decision"),
        Index("ix_risk_log_symbol_timestamp", "symbol", "timestamp"),
        {"implicit_returning": False},
    )

    def __repr__(self) -> str:
        return f"<RiskLog({self.decision.value} {self.symbol} {self.triggered_rule.value})>"


class DailyStats(Base):
    """Daily risk statistics tracking."""

    __tablename__ = "daily_stats"
    __table_args__ = {"implicit_returning": False}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, unique=True, index=True)

    starting_equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    current_equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    daily_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    daily_pnl_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))

    # Peak tracking for drawdown
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))

    # Exposure tracking
    total_exposure: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"), nullable=False)

    # Trade counts
    trades_count: Mapped[int] = mapped_column(default=0)
    winning_trades: Mapped[int] = mapped_column(default=0)
    losing_trades: Mapped[int] = mapped_column(default=0)

    # Limits
    daily_loss_limit_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    is_trading_halted: Mapped[bool] = mapped_column(default=False, nullable=False)
    halt_reason: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    def __repr__(self) -> str:
        return f"<DailyStats({self.date.date()} PnL={self.daily_pnl_pct}% DD={self.max_drawdown_pct}%)>"


class KillSwitch(Base):
    """Manual kill switch state."""

    __tablename__ = "kill_switch"
    __table_args__ = {"implicit_returning": False}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    is_active: Mapped[bool] = mapped_column(default=False, nullable=False)
    activated_by: Mapped[str | None] = mapped_column(String(100))  # "telegram", "api", "manual"
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<KillSwitch(active={self.is_active})>"


class GlobalRiskState(Base):
    """Estado global continuo para peak equity y drawdown acumulado (no resetea por día)."""

    __tablename__ = "global_risk_state"
    __table_args__ = {"implicit_returning": False}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)  # singleton
    peak_equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("10000"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    def __repr__(self) -> str:
        return f"<GlobalRiskState(peak={self.peak_equity})>"