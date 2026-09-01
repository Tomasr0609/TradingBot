"""Execution models - positions and order audit (Fase 4)."""

from datetime import datetime
from decimal import Decimal
from sqlalchemy import Integer, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from trading_bot.storage.models import Base


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = {"implicit_returning": False}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY/SELL
    size: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class ExecutedOrder(Base):
    """Auditoría completa de cada orden: señal -> riesgo -> orden -> respuesta Binance -> resultado."""

    __tablename__ = "executed_orders"
    __table_args__ = {"implicit_returning": False}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False, index=True)

    # Señal original
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(50), nullable=False)
    regime: Mapped[str] = mapped_column(String(20), nullable=False)
    atr: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))

    # Ajustes de riesgo
    risk_decision: Mapped[str] = mapped_column(String(20), nullable=False)  # approved/reduced/rejected
    risk_rule: Mapped[str] = mapped_column(String(30), nullable=False)
    risk_reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    trailing_stop: Mapped[bool] = mapped_column(default=False)

    # Orden enviada
    order_id: Mapped[str | None] = mapped_column(String(100))
    order_type: Mapped[str | None] = mapped_column(String(20))  # market/limit
    requested_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")  # pending/filled/rejected/error

    # Respuesta Binance
    executed_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    executed_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    raw_response: Mapped[str | None] = mapped_column(Text)  # JSON string

    # Stop loss real en exchange
    stop_loss_order_id: Mapped[str | None] = mapped_column(String(100))
    has_protection: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Resultado
    error_message: Mapped[str | None] = mapped_column(Text)
    is_testnet: Mapped[bool] = mapped_column(default=True, nullable=False)
