"""SQLAlchemy models for the trading bot."""

from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class Kline(Base):
    """Kline/candlestick data from Binance."""

    __tablename__ = "klines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    open_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    quote_volume: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    trades_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    taker_buy_base_volume: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    taker_buy_quote_volume: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    is_closed: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol", "timeframe", "open_time", name="uq_kline_symbol_tf_opentime"
        ),
        Index("ix_kline_symbol_tf_closed", "symbol", "timeframe", "is_closed"),
        {"implicit_returning": False},
    )

    def __repr__(self) -> str:
        return (
            f"<Kline(symbol={self.symbol}, tf={self.timeframe}, "
            f"open={self.open_time}, close={self.close_price})>"
        )