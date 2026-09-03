"""Backtesting module using vectorbt."""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional
import vectorbt as vbt

from trading_bot.decision.strategies import BaseStrategy, Signal, SignalType, get_strategy
from trading_bot.analysis.indicators import compute_indicators


@dataclass
class BacktestResult:
    """Container for backtest results."""
    strategy_name: str
    symbol: str
    timeframe: str
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    initial_capital: float
    final_value: float
    total_return: float
    total_return_pct: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    win_rate: float
    profit_factor: float
    num_trades: int
    avg_trade_return: float
    best_trade: float
    worst_trade: float
    equity_curve: pd.Series
    trades_df: pd.DataFrame
    metrics: dict


def run_backtest(
    df: pd.DataFrame,
    strategy: BaseStrategy,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    initial_capital: float = 10000,
    fees: float = 0.001,  # 0.1% per trade
    slippage: float = 0.0005,  # 0.05% slippage
    use_stop_loss: bool = True,
) -> BacktestResult:
    """
    Run vectorized backtest using vectorbt.

    CRITICAL: No look-ahead bias - signals at time t only use data up to t.
    """
    # Ensure DataFrame has datetime index
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("DataFrame must have DatetimeIndex")

    # Compute indicators (this is done at signal generation time in real trading)
    df = compute_indicators(df)

    # Generate signals (1, -1, 0)
    signals = strategy.generate_signals(df)

    # Verify no look-ahead: signals should only depend on past data
    # vectorbt handles this correctly when we pass signals as entries/exits

    # Convert signals to entry/exit arrays for vectorbt
    # entries: True where signal == 1 (buy)
    # exits: True where signal == -1 (sell)
    entries = signals == 1
    exits = signals == -1

    # Run portfolio backtest
    # We use close prices for execution (conservative)
    close_prices = df["close"]

    # Stop-loss real del bot: 2*ATR (mismo que risk_management/engine.py:339)
    # Convertido a porcentaje para vectorbt sl_stop (vectorbt espera % desde entry)
    # Si ATR varía por vela, sl_stop es un array alineado con entries
    # Solo se aplica si use_stop_loss True (para test comparativo sin stop)
    if use_stop_loss:
        if "atr_14" in df.columns:
            atr = df["atr_14"]
            # Evitar división por cero y NaNs del warmup
            sl_stop = (2 * atr) / close_prices
            sl_stop = sl_stop.replace([np.inf, -np.inf], np.nan).fillna(1.0).clip(lower=0.005, upper=0.5)
        else:
            # Fallback si por algún motivo no hay ATR (no debería pasar, compute_indicators siempre lo genera)
            sl_stop = 0.02
        portfolio_kwargs = dict(sl_stop=sl_stop)
    else:
        portfolio_kwargs = {}

    portfolio = vbt.Portfolio.from_signals(
        close=close_prices,
        entries=entries,
        exits=exits,
        init_cash=initial_capital,
        fees=fees,
        slippage=slippage,
        freq=timeframe,
        direction="both",  # Allow both long and short
        **portfolio_kwargs,
    )

    # Extract metrics
    stats = portfolio.stats()

    # Trade-level analysis
    trades = portfolio.trades.records_readable

    # Calculate additional metrics
    returns = portfolio.returns()
    equity_curve = portfolio.value()

    # Win rate
    if len(trades) > 0:
        winning_trades = trades[trades["PnL"] > 0]
        win_rate = len(winning_trades) / len(trades)
        profit_factor = (
            winning_trades["PnL"].sum() / abs(trades[trades["PnL"] < 0]["PnL"].sum())
            if len(trades[trades["PnL"] < 0]) > 0 else float('inf')
        )
        avg_trade_return = trades["Return"].mean()
        best_trade = trades["Return"].max()
        worst_trade = trades["Return"].min()
    else:
        win_rate = 0
        profit_factor = 0
        avg_trade_return = 0
        best_trade = 0
        worst_trade = 0

    # Sharpe / Sortino (annualized)
    # Assuming hourly data: 24 * 365 = 8760 periods per year
    periods_per_year = {"1m": 525600, "5m": 105120, "15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}
    ann_factor = np.sqrt(periods_per_year.get(timeframe, 8760))

    if len(returns) > 1 and returns.std() > 0:
        sharpe_ratio = returns.mean() / returns.std() * ann_factor
        # Sortino: only downside deviation
        downside_returns = returns[returns < 0]
        if len(downside_returns) > 0 and downside_returns.std() > 0:
            sortino_ratio = returns.mean() / downside_returns.std() * ann_factor
        else:
            sortino_ratio = float('inf')
    else:
        sharpe_ratio = 0
        sortino_ratio = 0

    # max_drawdown absoluto (dinero) no existe como "Max Drawdown" en vectorbt 0.27; calcular desde equity_curve
    running_max = equity_curve.cummax()
    drawdown_series = running_max - equity_curve
    max_drawdown = float(drawdown_series.max()) if len(drawdown_series) > 0 else 0.0

    return BacktestResult(
        strategy_name=strategy.name,
        symbol=symbol,
        timeframe=timeframe,
        start_date=df.index[0],
        end_date=df.index[-1],
        initial_capital=initial_capital,
        final_value=equity_curve.iloc[-1],
        total_return=equity_curve.iloc[-1] - initial_capital,
        total_return_pct=(equity_curve.iloc[-1] / initial_capital - 1) * 100,
        max_drawdown=max_drawdown,
        max_drawdown_pct=stats["Max Drawdown [%]"],
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        win_rate=win_rate,
        profit_factor=profit_factor,
        num_trades=len(trades),
        avg_trade_return=avg_trade_return,
        best_trade=best_trade,
        worst_trade=worst_trade,
        equity_curve=equity_curve,
        trades_df=trades,
        metrics=stats.to_dict(),
    )


def run_walk_forward_backtest(
    df: pd.DataFrame,
    strategy: BaseStrategy,
    train_window: str = "6M",
    test_window: str = "1M",
    step: str = "1M",
    **kwargs,
) -> list[BacktestResult]:
    """
    Run walk-forward backtest (rolling window).

    This is more robust than single backtest as it tests parameter stability.
    """
    results = []

    # Generate date ranges
    start = df.index[0]
    end = df.index[-1]

    current = start
    while current < end:
        train_end = current + pd.Timedelta(train_window)
        test_end = train_end + pd.Timedelta(test_window)

        if test_end > end:
            break

        train_df = df[(df.index >= current) & (df.index < train_end)]
        test_df = df[(df.index >= train_end) & (df.index < test_end)]

        if len(test_df) < 10:
            break

        # Run backtest on test period
        # Note: In production, you'd optimize parameters on train_df here
        result = run_backtest(test_df, strategy, **kwargs)
        results.append(result)

        current += pd.Timedelta(step)

    return results


def optimize_strategy_parameters(
    df: pd.DataFrame,
    strategy_class,
    param_grid: dict,
    metric: str = "sharpe_ratio",
    **backtest_kwargs,
) -> tuple[dict, BacktestResult]:
    """
    Grid search optimization of strategy parameters.

    WARNING: Prone to overfitting. Use walk-forward validation.
    """
    best_params = None
    best_result = None
    best_metric = -np.inf

    # Generate parameter combinations
    import itertools
    keys = list(param_grid.keys())
    values = list(param_grid.values())

    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        strategy = strategy_class(**params)

        result = run_backtest(df, strategy, **backtest_kwargs)

        metric_value = getattr(result, metric, 0)
        if metric_value > best_metric:
            best_metric = metric_value
            best_params = params
            best_result = result

    return best_params, best_result


def print_backtest_report(result: BacktestResult) -> None:
    """Print formatted backtest report."""
    print(f"\n{'='*60}")
    print(f"BACKTEST REPORT: {result.strategy_name} on {result.symbol} {result.timeframe}")
    print(f"{'='*60}")
    print(f"Period: {result.start_date} to {result.end_date}")
    print(f"Initial Capital: ${result.initial_capital:,.2f}")
    print(f"Final Value: ${result.final_value:,.2f}")
    print(f"Total Return: ${result.total_return:,.2f} ({result.total_return_pct:.2f}%)")
    print(f"Max Drawdown: ${result.max_drawdown:,.2f} ({result.max_drawdown_pct:.2f}%)")
    print(f"Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"Sortino Ratio: {result.sortino_ratio:.2f}")
    print(f"Win Rate: {result.win_rate*100:.1f}%")
    print(f"Profit Factor: {result.profit_factor:.2f}")
    print(f"Number of Trades: {result.num_trades}")
    print(f"Avg Trade Return: {result.avg_trade_return*100:.2f}%")
    print(f"Best Trade: {result.best_trade*100:.2f}%")
    print(f"Worst Trade: {result.worst_trade*100:.2f}%")
    print(f"{'='*60}\n")