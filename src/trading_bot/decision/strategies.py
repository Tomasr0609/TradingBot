"""Trading strategies - pure functions that generate signals from data."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import pandas as pd

from trading_bot.analysis.indicators import (
    compute_indicators,
    get_market_regime,
    sma_crossover_signal,
    ema_crossover_signal,
    bollinger_mean_reversion_signal,
)


class SignalType(Enum):
    BUY = 1
    SELL = -1
    HOLD = 0


@dataclass
class Signal:
    """Trading signal with metadata."""
    symbol: str
    timestamp: pd.Timestamp
    signal_type: SignalType
    price: float
    strategy_name: str
    regime: str
    indicators: dict
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    def __post_init__(self):
        if isinstance(self.signal_type, int):
            self.signal_type = SignalType(self.signal_type)


class BaseStrategy:
    """Base class for all strategies."""

    name: str = "base"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Generate signals from DataFrame. Returns Series with 1, -1, 0."""
        raise NotImplementedError

    def get_stop_loss(self, df: pd.DataFrame, signal_idx: int, signal_type: SignalType) -> Optional[float]:
        """Calculate stop loss for a signal. Override in subclasses."""
        return None

    def get_take_profit(self, df: pd.DataFrame, signal_idx: int, signal_type: SignalType) -> Optional[float]:
        """Calculate take profit for a signal. Override in subclasses."""
        return None


class SMACrossoverStrategy(BaseStrategy):
    """
    SMA Crossover Strategy with RSI filter.

    Buy: SMA20 crosses above SMA50 AND RSI < 70
    Sell: SMA20 crosses below SMA50 AND RSI > 30

    Adapts to regime:
    - Trend: Use standard crossover
    - Range: Disable (too many whipsaws)
    """

    name = "sma_crossover_rsi"

    def __init__(self, disable_in_range: bool = True):
        self.disable_in_range = disable_in_range

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = compute_indicators(df)
        regime = get_market_regime(df)

        if self.disable_in_range and regime == "range":
            return pd.Series(0, index=df.index)

        return sma_crossover_signal(df)

    def get_stop_loss(self, df: pd.DataFrame, signal_idx: int, signal_type: SignalType) -> Optional[float]:
        """ATR-based stop loss."""
        if "atr_14" not in df.columns:
            return None

        atr = df["atr_14"].iloc[signal_idx]
        close = df["close"].iloc[signal_idx]

        if signal_type == SignalType.BUY:
            return close - 2 * atr
        elif signal_type == SignalType.SELL:
            return close + 2 * atr
        return None


class EMACrossoverMACDStrategy(BaseStrategy):
    """
    EMA Crossover with MACD confirmation.

    Buy: EMA20 crosses above EMA50 AND MACD crosses above signal
    Sell: EMA20 crosses below EMA50 AND MACD crosses below signal
    """

    name = "ema_crossover_macd"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = compute_indicators(df)
        return ema_crossover_signal(df)

    def get_stop_loss(self, df: pd.DataFrame, signal_idx: int, signal_type: SignalType) -> Optional[float]:
        """ATR-based stop loss."""
        if "atr_14" not in df.columns:
            return None

        atr = df["atr_14"].iloc[signal_idx]
        close = df["close"].iloc[signal_idx]

        if signal_type == SignalType.BUY:
            return close - 1.5 * atr
        elif signal_type == SignalType.SELL:
            return close + 1.5 * atr
        return None


class BollingerMeanReversionStrategy(BaseStrategy):
    """
    Bollinger Bands Mean Reversion - ONLY works in range markets.

    Buy: Price at lower band + RSI < 35
    Sell: Price at upper band + RSI > 65

    Automatically disabled in trending markets.
    """

    name = "bollinger_mean_reversion"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = compute_indicators(df)
        return bollinger_mean_reversion_signal(df)

    def get_stop_loss(self, df: pd.DataFrame, signal_idx: int, signal_type: SignalType) -> Optional[float]:
        """Stop at middle band for mean reversion."""
        if "bb_middle" not in df.columns:
            return None

        middle = df["bb_middle"].iloc[signal_idx]
        close = df["close"].iloc[signal_idx]

        if signal_type == SignalType.BUY:
            return min(close - 0.02 * close, middle)  # 2% or middle band
        elif signal_type == SignalType.SELL:
            return max(close + 0.02 * close, middle)
        return None


class CompositeStrategy(BaseStrategy):
    """
    Composite strategy that combines multiple strategies with regime awareness.

    - Trend regime: Use trend-following (SMA/EMA crossover)
    - Range regime: Use mean reversion (Bollinger)
    - Transitional: No signals
    """

    name = "composite_regime_aware"

    def __init__(self):
        self.trend_strategy = SMACrossoverStrategy(disable_in_range=True)
        self.range_strategy = BollingerMeanReversionStrategy()

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = compute_indicators(df)
        regime = get_market_regime(df)

        if regime in ("trend", "transitional"):
            return self.trend_strategy.generate_signals(df)
        elif regime == "range":
            return self.range_strategy.generate_signals(df)
        else:
            return pd.Series(0, index=df.index)

    def get_stop_loss(self, df: pd.DataFrame, signal_idx: int, signal_type: SignalType) -> Optional[float]:
        regime = get_market_regime(df)
        if regime == "trend":
            return self.trend_strategy.get_stop_loss(df, signal_idx, signal_type)
        elif regime == "range":
            return self.range_strategy.get_stop_loss(df, signal_idx, signal_type)
        return None


def get_strategy(name: str) -> BaseStrategy:
    """Factory function to get strategy by name."""
    strategies = {
        "sma_crossover": SMACrossoverStrategy(),
        "ema_macd": EMACrossoverMACDStrategy(),
        "bollinger_mr": BollingerMeanReversionStrategy(),
        "composite": CompositeStrategy(),
    }
    if name not in strategies:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(strategies.keys())}")
    return strategies[name]


def signals_to_objects(
    df: pd.DataFrame,
    signals: pd.Series,
    strategy: BaseStrategy,
    symbol: str,
) -> list[Signal]:
    """Convert signal series to Signal objects with metadata."""
    df = compute_indicators(df)
    regime = get_market_regime(df)

    signal_objects = []
    for idx, sig_val in signals.items():
        if sig_val == 0:
            continue

        signal_type = SignalType(sig_val)
        price = df.loc[idx, "close"]

        # Get indicator snapshot at signal time
        indicators = {
            "rsi": df.loc[idx, "rsi_14"] if "rsi_14" in df.columns else None,
            "atr": df.loc[idx, "atr_14"] if "atr_14" in df.columns else None,
            "adx": df.loc[idx, "adx_14"] if "adx_14" in df.columns else None,
            "macd": df.loc[idx, "macd"] if "macd" in df.columns else None,
        }

        stop_loss = strategy.get_stop_loss(df, df.index.get_loc(idx), signal_type)

        signal_objects.append(Signal(
            symbol=symbol,
            timestamp=idx,
            signal_type=signal_type,
            price=price,
            strategy_name=strategy.name,
            regime=regime,
            indicators=indicators,
            stop_loss=stop_loss,
        ))

    return signal_objects