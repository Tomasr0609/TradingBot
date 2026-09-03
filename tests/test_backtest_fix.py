"""Tests para corrección vectorbt/plotly y cálculo max_drawdown absoluto."""

import pandas as pd
import numpy as np
import pytest


def make_synthetic_klines(n=150):
    dates = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    np.random.seed(42)
    # Tendencia con pullbacks para generar señales en todas las estrategias
    drift = 0.001
    vol = 0.01
    returns = np.random.normal(drift, vol, n)
    close = 100 * np.exp(np.cumsum(returns))
    # Forzar pullbacks cada 40 para generar crossovers
    for i in range(40, n, 50):
        close[i:] *= 0.93
    high = close * 1.005
    low = close * 0.995
    open_p = close * 1.001
    volume = np.random.uniform(100, 1000, n)
    df = pd.DataFrame({"open": open_p, "high": high, "low": low, "close": close, "volume": volume}, index=dates)
    return df


def test_vectorbt_plotly_compatible():
    """plotly debe ser <6 para ser compatible con vectorbt."""
    import plotly
    from packaging import version
    assert version.parse(plotly.__version__) < version.parse("6.0.0"), f"plotly {plotly.__version__} debe ser <6"

def test_vectorbt_imports_without_scattermapbox_error():
    """import vectorbt no debe lanzar ValueError scattermapbox."""
    import vectorbt as vbt  # noqa
    assert vbt is not None

def test_max_drawdown_calculado_en_dinero():
    """Verifica que max_drawdown es valor absoluto (dinero), no porcentaje, y coincide con cálculo manual."""
    from trading_bot.backtesting.engine import run_backtest
    from trading_bot.decision.strategies import BaseStrategy
    import pandas as pd

    # Estrategia dummy que solo compra y mantiene (long-only) para curva predecible sin shorts
    class DummyLongStrategy(BaseStrategy):
        name = "dummy_long"
        def generate_signals(self, df):
            s = pd.Series(0, index=df.index)
            s.iloc[0] = 1  # buy at 100
            # no sell, mantiene posición hasta el final
            return s

    # Precios: 100 -> 150 -> 100 -> 120 -> 80 -> luego 120 constante
    # Equity = 10000*price/100 -> 10000, 15000, 10000, 12000, 8000 ...
    n = 100
    dates = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    close = np.concatenate([np.array([100, 150, 100, 120, 80]), np.full(n-5, 120)])
    df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 100}, index=dates)

    strategy = DummyLongStrategy()
    result = run_backtest(df, strategy, symbol="TEST/USDT", timeframe="1h", initial_capital=10000, fees=0, slippage=0)

    eq = result.equity_curve
    running_max = eq.cummax()
    expected = float((running_max - eq).max())
    assert abs(result.max_drawdown - expected) < 1e-6, f"max_drawdown {result.max_drawdown} != expected {expected}"
    # Debe ser en dinero, no porcentaje, y menor al capital para este caso long-only
    assert 0 <= result.max_drawdown < result.initial_capital
    assert result.max_drawdown != result.max_drawdown_pct
    assert 0 <= result.max_drawdown_pct <= 100
    assert "max_drawdown" in result.__dict__

def test_run_backtest_no_keyerror_para_4_estrategias():
    """Las 4 estrategias no deben lanzar KeyError Max Drawdown."""
    from trading_bot.backtesting.engine import run_backtest
    from trading_bot.decision.strategies import get_strategy
    import math
    df = make_synthetic_klines(150)
    for name in ["sma_crossover", "ema_macd", "bollinger_mr", "composite"]:
        strategy = get_strategy(name)
        result = run_backtest(df, strategy, symbol="BTC/USDT", timeframe="1h", initial_capital=10000)
        assert hasattr(result, "max_drawdown")
        assert hasattr(result, "max_drawdown_pct")
        assert isinstance(result.max_drawdown, (int, float))
        assert result.max_drawdown >= 0
        assert result.max_drawdown < result.initial_capital * 2
        # Si no hay trades, max_drawdown_pct puede ser NaN (vectorbt)
        if result.num_trades == 0:
            assert math.isnan(result.max_drawdown_pct) or 0 <= result.max_drawdown_pct <= 100
        else:
            assert 0 <= result.max_drawdown_pct <= 100

def test_pyproject_vectorbt_plotly_pin():
    """pyproject.toml debe tener plotly<6 en extra vectorbt."""
    import pathlib
    text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
    assert "vectorbt" in text
    assert "plotly<6" in text
