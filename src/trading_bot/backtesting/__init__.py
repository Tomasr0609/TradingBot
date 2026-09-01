"""Backtesting Module - Vectorized backtesting with vectorbt."""

from trading_bot.backtesting.engine import (
    BacktestResult,
    run_backtest,
    run_walk_forward_backtest,
    optimize_strategy_parameters,
    print_backtest_report,
)

__all__ = [
    "BacktestResult",
    "run_backtest",
    "run_walk_forward_backtest",
    "optimize_strategy_parameters",
    "print_backtest_report",
]