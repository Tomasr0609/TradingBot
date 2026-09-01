"""Decision Module - Trading strategies and signal generation."""

from trading_bot.decision.strategies import (
    BaseStrategy,
    Signal,
    SignalType,
    SMACrossoverStrategy,
    EMACrossoverMACDStrategy,
    BollingerMeanReversionStrategy,
    CompositeStrategy,
    get_strategy,
    signals_to_objects,
)

__all__ = [
    "BaseStrategy",
    "Signal",
    "SignalType",
    "SMACrossoverStrategy",
    "EMACrossoverMACDStrategy",
    "BollingerMeanReversionStrategy",
    "CompositeStrategy",
    "get_strategy",
    "signals_to_objects",
]