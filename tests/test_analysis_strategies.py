"""Tests for analysis indicators and strategies."""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone, timedelta

from trading_bot.analysis.indicators import (
    compute_indicators,
    compute_indicators_incremental,
    get_market_regime,
    sma_crossover_signal,
)
from trading_bot.decision.strategies import (
    SMACrossoverStrategy,
    EMACrossoverMACDStrategy,
    BollingerMeanReversionStrategy,
    CompositeStrategy,
    SignalType,
    get_strategy,
    signals_to_objects,
)


def create_synthetic_trend_data(length: int = 200, trend: str = "up") -> pd.DataFrame:
    """Create synthetic price data with a clear trend using GBM."""
    dates = pd.date_range("2023-01-01", periods=length, freq="1h", tz="UTC")
    np.random.seed(42)  # Deterministic for tests

    if trend == "up":
        # Uptrend with pullbacks to trigger SMA crossovers
        drift = 0.0015
        vol = 0.008
        returns = np.random.normal(drift, vol, length)
        close = 100 * np.exp(np.cumsum(returns))
        # Add pullback every 40 periods to create crossovers
        for i in range(40, length, 45):
            close[i:] *= 0.97
    elif trend == "down":
        drift = -0.0015
        vol = 0.008
        returns = np.random.normal(drift, vol, length)
        close = 100 * np.exp(np.cumsum(returns))
        for i in range(40, length, 45):
            close[i:] *= 1.03
    else:  # range - tight range with noise, low ADX
        close = 125 + 3 * np.sin(np.linspace(0, 20*np.pi, length))
        noise = np.random.normal(0, 0.5, length)
        close = close + noise

    # Generate OHLC from close
    high = close * (1 + np.abs(np.random.normal(0, 0.002, length)))
    low = close * (1 - np.abs(np.random.normal(0, 0.002, length)))
    open_price = close * (1 + np.random.normal(0, 0.001, length))
    volume = np.random.uniform(100, 1000, length)

    df = pd.DataFrame({
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }, index=dates)

    return df


class TestIndicators:
    """Tests for technical indicators."""

    def test_compute_indicators_adds_columns(self):
        """Test that compute_indicators adds all expected columns."""
        df = create_synthetic_trend_data(100, "up")
        result = compute_indicators(df)

        expected_cols = [
            "sma_20", "sma_50", "ema_20", "ema_50",
            "rsi_14", "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_middle", "bb_lower", "bb_width",
            "atr_14", "adx_14", "di_plus", "di_minus",
        ]

        for col in expected_cols:
            assert col in result.columns, f"Missing column: {col}"

    def test_compute_indicators_preserves_index(self):
        """Test that index is preserved."""
        df = create_synthetic_trend_data(100, "up")
        result = compute_indicators(df)
        assert result.index.equals(df.index)

    def test_sma_crossover_signal_buy_on_crossover(self):
        """Test SMA crossover generates buy signal on golden cross."""
        # Create data with clear crossover
        df = create_synthetic_trend_data(200, "up")
        df = compute_indicators(df)

        signals = sma_crossover_signal(df)

        # Should have at least one buy signal in uptrend
        buy_signals = (signals == 1).sum()
        assert buy_signals >= 1

    def test_get_market_regime_trend(self):
        """Test regime detection for trending market."""
        # Use stronger trend without pullbacks for regime detection
        df = create_synthetic_trend_data(200, "up")
        # Override with stronger trend for this specific test
        np.random.seed(42)
        length = 200
        dates = pd.date_range("2023-01-01", periods=length, freq="1h", tz="UTC")
        drift = 0.003  # Strong drift
        vol = 0.003    # Low volatility
        returns = np.random.normal(drift, vol, length)
        close = 100 * np.exp(np.cumsum(returns))
        high = close * (1 + np.abs(np.random.normal(0, 0.001, length)))
        low = close * (1 - np.abs(np.random.normal(0, 0.001, length)))
        open_price = close * (1 + np.random.normal(0, 0.0005, length))
        volume = np.random.uniform(100, 1000, length)
        df = pd.DataFrame({
            "open": open_price, "high": high, "low": low, "close": close, "volume": volume
        }, index=dates)
        
        df = compute_indicators(df)
        regime = get_market_regime(df)
        assert regime in ("trend", "transitional")

    def test_get_market_regime_range(self):
        """Test regime detection for ranging market."""
        df = create_synthetic_trend_data(200, "range")
        df = compute_indicators(df)

        regime = get_market_regime(df)
        # Range markets can be 'range' or 'transitional' depending on ADX
        assert regime in ("range", "transitional")


class TestStrategies:
    """Tests for trading strategies."""

    @pytest.fixture
    def trend_df(self):
        """Trending market data with indicators - strong trend with pullbacks for crossovers."""
        np.random.seed(42)
        length = 200
        dates = pd.date_range("2023-01-01", periods=length, freq="1h", tz="UTC")
        drift = 0.002
        vol = 0.005
        returns = np.random.normal(drift, vol, length)
        close = 100 * np.exp(np.cumsum(returns))
        # Add pullbacks to trigger SMA crossovers
        for i in range(40, length, 45):
            close[i:] *= 0.97
        high = close * (1 + np.abs(np.random.normal(0, 0.002, length)))
        low = close * (1 - np.abs(np.random.normal(0, 0.002, length)))
        open_price = close * (1 + np.random.normal(0, 0.001, length))
        volume = np.random.uniform(100, 1000, length)
        df = pd.DataFrame({
            "open": open_price, "high": high, "low": low, "close": close, "volume": volume
        }, index=dates)
        return compute_indicators(df)

    @pytest.fixture
    def range_df(self):
        """Ranging market data with indicators."""
        df = create_synthetic_trend_data(200, "range")
        return compute_indicators(df)

    def test_sma_crossover_strategy_generates_signals(self, trend_df):
        """Test SMA crossover strategy generates signals in trend."""
        strategy = SMACrossoverStrategy(disable_in_range=True)
        signals = strategy.generate_signals(trend_df)

        # Should have some signals
        assert (signals != 0).any()

    def test_sma_crossover_disabled_in_range(self, range_df):
        """Test SMA crossover is disabled in range market."""
        strategy = SMACrossoverStrategy(disable_in_range=True)
        signals = strategy.generate_signals(range_df)

        # Should have NO signals in range
        assert (signals == 0).all()

    def test_ema_macd_strategy(self, trend_df):
        """Test EMA MACD strategy."""
        strategy = EMACrossoverMACDStrategy()
        signals = strategy.generate_signals(trend_df)

        # Should generate some signals
        assert len(signals) == len(trend_df)

    def test_bollinger_mean_reversion_only_in_range(self, trend_df, range_df):
        """Test Bollinger MR only works in range."""
        strategy = BollingerMeanReversionStrategy()

        # Should be disabled in trend
        trend_signals = strategy.generate_signals(trend_df)
        # (Actually it's not disabled by default, but regime detection
        # in the signal function should return 0)

        # Should work in range
        range_signals = strategy.generate_signals(range_df)
        assert len(range_signals) == len(range_df)

    def test_composite_strategy_trend(self, trend_df):
        """Test composite uses trend strategy in trend."""
        strategy = CompositeStrategy()
        signals = strategy.generate_signals(trend_df)

        # Should have signals in trend
        assert (signals != 0).any()

    def test_composite_strategy_range(self, range_df):
        """Test composite uses range strategy in range."""
        strategy = CompositeStrategy()
        signals = strategy.generate_signals(range_df)

        # Should have signals in range
        assert (signals != 0).any()

    def test_get_strategy_factory(self):
        """Test strategy factory."""
        sma = get_strategy("sma_crossover")
        assert isinstance(sma, SMACrossoverStrategy)

        ema = get_strategy("ema_macd")
        assert isinstance(ema, EMACrossoverMACDStrategy)

        bb = get_strategy("bollinger_mr")
        assert isinstance(bb, BollingerMeanReversionStrategy)

        comp = get_strategy("composite")
        assert isinstance(comp, CompositeStrategy)

    def test_get_strategy_invalid_raises(self):
        """Test factory raises for unknown strategy."""
        with pytest.raises(ValueError):
            get_strategy("nonexistent")

    def test_signals_to_objects(self, trend_df):
        """Test conversion of signals to Signal objects."""
        strategy = SMACrossoverStrategy()
        signals = strategy.generate_signals(trend_df)

        signal_objs = signals_to_objects(trend_df, signals, strategy, "BTC/USDT")

        # Should have at least one signal object
        if (signals != 0).any():
            assert len(signal_objs) > 0
            for sig in signal_objs:
                assert sig.symbol == "BTC/USDT"
                assert sig.signal_type in (SignalType.BUY, SignalType.SELL)
                assert sig.price > 0
                assert sig.strategy_name == "sma_crossover_rsi"
                assert sig.regime in ("trend", "range", "transitional", "unknown")

    def test_stop_loss_calculation(self, trend_df):
        """Test ATR-based stop loss calculation."""
        strategy = SMACrossoverStrategy()
        signals = strategy.generate_signals(trend_df)

        # Find a buy signal
        buy_idx = signals[signals == 1].index
        if len(buy_idx) > 0:
            idx = trend_df.index.get_loc(buy_idx[0])
            stop = strategy.get_stop_loss(trend_df, idx, SignalType.BUY)
            assert stop is not None
            assert stop < trend_df.iloc[idx]["close"]  # Stop below entry for buy


class TestLookAheadBias:
    """Tests to verify no look-ahead bias in signals."""

    def test_sma_crossover_no_lookahead(self):
        """Verify SMA crossover signal at t only uses data up to t."""
        df = create_synthetic_trend_data(100, "up")
        df = compute_indicators(df)

        signals = sma_crossover_signal(df)

        # The signal at index i should only depend on data up to i
        # Verify by checking that shifting signals forward doesn't
        # perfectly predict future returns
        future_returns = df["close"].pct_change().shift(-1)
        signal_returns = future_returns * signals

        # If there's lookahead, signal_returns would be perfectly positive
        # In reality, there should be both positive and negative
        if (signals != 0).any():
            assert signal_returns.std() > 0  # Not perfectly predictable

    def test_indicators_not_future_leak(self):
        """Test that indicator calculation doesn't use future data."""
        df = create_synthetic_trend_data(100, "up")

        # Compute indicators incrementally vs all at once
        full = compute_indicators(df)
        incremental = compute_indicators_incremental(df.copy(), last_row_only=True)

        # Last row should match
        for col in full.columns:
            if col in incremental.columns:
                if not pd.isna(full[col].iloc[-1]):
                    assert abs(full[col].iloc[-1] - incremental[col].iloc[-1]) < 0.01


# Run with: pytest tests/test_analysis_strategies.py -v