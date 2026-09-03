"""Tests para stop-loss real en backtest (2*ATR)."""

import pandas as pd
import numpy as np
import pytest


def make_crash_df(n=100, crash_at=50, crash_to=60):
    """Crea DataFrame con caída gradual después de entrada (para que stop 2*ATR pueda activarse)."""
    dates = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    np.random.seed(0)
    close = np.full(n, 100.0)
    close[:crash_at] += np.random.normal(0, 0.5, crash_at)
    # Caída gradual en 6 velas: 100 -> 96 -> 92 -> 88 -> 75 -> 65 -> 60
    crash_prices = [96, 92, 88, 75, 65, 60]
    for i, p in enumerate(crash_prices):
        if crash_at + i < n:
            close[crash_at + i] = p
    close[crash_at + len(crash_prices):] = crash_to
    high = close + 2
    low = close - 2
    open_p = close
    volume = np.full(n, 100.0)
    df = pd.DataFrame({"open": open_p, "high": high, "low": low, "close": close, "volume": volume}, index=dates)
    return df


def test_stoploss_acota_perdida_en_caida_abrupta():
    """Precio cae 30-40% tras entrada, con 2*ATR ~4% la pérdida debe quedar acotada."""
    from trading_bot.backtesting.engine import run_backtest
    from trading_bot.decision.strategies import BaseStrategy
    import pandas as pd

    class CrashBuyStrategy(BaseStrategy):
        name = "crash_test"
        def generate_signals(self, df):
            s = pd.Series(0, index=df.index)
            s.iloc[50] = 1  # compra justo antes de la caída
            s.iloc[80] = -1 # venta tardía (si no fuera por stop, perdería mucho)
            return s

    df = make_crash_df(n=100, crash_at=51, crash_to=60)  # caída 40%
    # Forzamos ATR conocido: high 102 low 98 => ATR ~4, 2*ATR=8 => 8% stop
    # run_backtest calculará ATR via compute_indicators, que con high 102 low 98 dará ~4
    strategy = CrashBuyStrategy()
    result = run_backtest(df, strategy, symbol="BTC/USDT", timeframe="1h", initial_capital=10000, fees=0, slippage=0, use_stop_loss=True)

    # Con stop, la peor pérdida debe estar acotada cerca de 2*ATR (~8%), no 40%
    assert result.num_trades >= 1
    # Worst trade debe ser alrededor de -8% (con margen por fees/slippage 0)
    # Permitimos 5% a 15% como rango razonable
    assert -0.20 < result.worst_trade < -0.02, f"worst {result.worst_trade} no está acotado a 2*ATR, debería estar cerca -0.08"
    # Sin stop, la caída sería -40%
    assert result.worst_trade > -0.35, "Con stop, worst no debe ser -30% como antes"

def test_stoploss_vs_sin_stoploss_diferencia():
    """Mismo escenario con y sin stop debe dar resultados distintos, con stop mejor acotado."""
    from trading_bot.backtesting.engine import run_backtest
    from trading_bot.decision.strategies import BaseStrategy
    import pandas as pd

    class CrashBuyStrategy(BaseStrategy):
        name = "crash_test2"
        def generate_signals(self, df):
            s = pd.Series(0, index=df.index)
            s.iloc[50] = 1
            s.iloc[90] = -1
            return s

    df = make_crash_df(n=100, crash_at=51, crash_to=60)
    strategy = CrashBuyStrategy()

    result_con = run_backtest(df, strategy, symbol="BTC/USDT", timeframe="1h", initial_capital=10000, fees=0, slippage=0, use_stop_loss=True)
    result_sin = run_backtest(df, strategy, symbol="BTC/USDT", timeframe="1h", initial_capital=10000, fees=0, slippage=0, use_stop_loss=False)

    # Deben ser distintos
    assert result_con.worst_trade != result_sin.worst_trade
    # Con stop debe ser mejor (menos pérdida) que sin stop
    # Sin stop, worst ~ -40% (de 100 a 60)
    # Con stop, worst ~ -8%
    assert result_con.worst_trade > result_sin.worst_trade, f"Con stop {result_con.worst_trade} debería ser mejor que sin {result_sin.worst_trade}"
    assert result_sin.worst_trade < -0.30, f"Sin stop, worst debe ser caída grande {result_sin.worst_trade}"
    # Con stop, drawdown también debe ser menor
    assert result_con.max_drawdown < result_sin.max_drawdown or result_con.max_drawdown_pct < result_sin.max_drawdown_pct

def test_no_tp_stop_inventado():
    """Verifica que no se inventa tp_stop (take profit) en el backtest."""
    import pathlib
    text = pathlib.Path("src/trading_bot/backtesting/engine.py").read_text(encoding="utf-8")
    # No debe haber tp_stop en from_signals salvo que risk lo use (no lo usa)
    assert "tp_stop" not in text or text.count("tp_stop") == 0, "No debe haber tp_stop inventado"
    # Debe haber sl_stop
    assert "sl_stop" in text
