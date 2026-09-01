"""Technical indicators using pandas-ta."""

import pandas as pd
import pandas_ta as ta
from trading_bot.config.settings import get_settings

# Cache for indicator calculations to avoid recomputation
_indicator_cache: dict = {}


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all technical indicators on a DataFrame with OHLCV data.

    Expected columns: open, high, low, close, volume
    Index: datetime (timezone-aware)

    Returns:
        DataFrame with added indicator columns
    """
    if df.empty:
        return df

    df = df.copy()

    # Ensure we have the required columns
    required = ["open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")

    # Trend indicators
    df["sma_20"] = ta.sma(df["close"], length=20)
    df["sma_50"] = ta.sma(df["close"], length=50)
    df["ema_20"] = ta.ema(df["close"], length=20)
    df["ema_50"] = ta.ema(df["close"], length=50)

    # Momentum
    df["rsi_14"] = ta.rsi(df["close"], length=14)

    # MACD
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"] = macd["MACD_12_26_9"]
        df["macd_signal"] = macd["MACDs_12_26_9"]
        df["macd_hist"] = macd["MACDh_12_26_9"]

    # Bollinger Bands - handle different pandas-ta versions
    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None:
        # pandas-ta v0.4+ uses BBU_20_2.0_2.0 format, older uses BBU_20_2.0
        upper_col = [c for c in bb.columns if c.startswith("BBU")][0]
        middle_col = [c for c in bb.columns if c.startswith("BBM")][0]
        lower_col = [c for c in bb.columns if c.startswith("BBL")][0]

        df["bb_upper"] = bb[upper_col]
        df["bb_middle"] = bb[middle_col]
        df["bb_lower"] = bb[lower_col]
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]

    # ATR for volatility and position sizing
    df["atr_14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ADX for trend strength
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx is not None:
        df["adx_14"] = adx["ADX_14"]
        df["di_plus"] = adx["DMP_14"]
        df["di_minus"] = adx["DMN_14"]

    return df


def compute_indicators_incremental(
    df: pd.DataFrame,
    last_row_only: bool = False,
) -> pd.DataFrame:
    """
    Compute indicators incrementally (only new rows).

    For use with streaming data - only computes indicators for the last N rows
    to avoid recomputing everything.
    """
    if last_row_only and len(df) > 50:
        # Only compute on last 100 rows for efficiency
        # (need enough history for indicators like SMA_50)
        recent = df.iloc[-100:].copy()
        recent = compute_indicators(recent)
        # Update only the last row in original df
        for col in recent.columns:
            if col not in df.columns:
                df[col] = None
        df.iloc[-1] = recent.iloc[-1]
        return df
    else:
        return compute_indicators(df)


def get_market_regime(df: pd.DataFrame, lookback: int = 50) -> str:
    """
    Detect market regime: 'trend' or 'range'.

    Uses ADX + DI crossover + price vs MA position.
    """
    if len(df) < lookback:
        return "unknown"

    recent = df.iloc[-lookback:]

    # ADX trend strength
    adx = recent["adx_14"].iloc[-1] if "adx_14" in recent.columns else 0
    di_plus = recent["di_plus"].iloc[-1] if "di_plus" in recent.columns else 0
    di_minus = recent["di_minus"].iloc[-1] if "di_minus" in recent.columns else 0

    # Price vs SMA position
    close = recent["close"].iloc[-1]
    sma_20 = recent["sma_20"].iloc[-1] if "sma_20" in recent.columns else close
    sma_50 = recent["sma_50"].iloc[-1] if "sma_50" in recent.columns else close

    # Strong trend: ADX > 15 and DI separation
    if adx > 15 and abs(di_plus - di_minus) > 5:
        return "trend"

    # Weak trend / range: ADX < 22
    if adx < 22:
        return "range"

    # Transitional
    return "transitional"


def sma_crossover_signal(df: pd.DataFrame) -> pd.Series:
    """
    Generate signals based on SMA crossover with RSI confirmation.

    Returns:
        Series with values: 1 (buy), -1 (sell), 0 (hold)
    """
    signals = pd.Series(0, index=df.index)

    # Need at least 2 rows for crossover detection
    if len(df) < 2:
        return signals

    # Fast SMA crosses above slow SMA + RSI not overbought
    fast_above_slow = (df["sma_20"] > df["sma_50"]) & (df["sma_20"].shift(1) <= df["sma_50"].shift(1))
    rsi_ok_buy = df["rsi_14"] < 70

    # Fast SMA crosses below slow SMA + RSI not oversold
    fast_below_slow = (df["sma_20"] < df["sma_50"]) & (df["sma_20"].shift(1) >= df["sma_50"].shift(1))
    rsi_ok_sell = df["rsi_14"] > 30

    signals[fast_above_slow & rsi_ok_buy] = 1
    signals[fast_below_slow & rsi_ok_sell] = -1

    return signals


def ema_crossover_signal(df: pd.DataFrame) -> pd.Series:
    """
    Generate signals based on EMA crossover with MACD confirmation.
    """
    signals = pd.Series(0, index=df.index)

    if len(df) < 2:
        return signals

    # EMA crossover
    fast_above_slow = (df["ema_20"] > df["ema_50"]) & (df["ema_20"].shift(1) <= df["ema_50"].shift(1))
    fast_below_slow = (df["ema_20"] < df["ema_50"]) & (df["ema_20"].shift(1) >= df["ema_50"].shift(1))

    # MACD confirmation
    macd_bullish = (df["macd"] > df["macd_signal"]) & (df["macd"].shift(1) <= df["macd_signal"].shift(1))
    macd_bearish = (df["macd"] < df["macd_signal"]) & (df["macd"].shift(1) >= df["macd_signal"].shift(1))

    signals[fast_above_slow & macd_bullish] = 1
    signals[fast_below_slow & macd_bearish] = -1

    return signals


def bollinger_mean_reversion_signal(df: pd.DataFrame) -> pd.Series:
    """
    Mean reversion signals using Bollinger Bands.

    Buy when price touches lower band in range market.
    Sell when price touches upper band in range market.
    """
    signals = pd.Series(0, index=df.index)

    if "bb_upper" not in df.columns or "bb_lower" not in df.columns:
        return signals

    # Only in range markets
    regime = get_market_regime(df)
    if regime != "range":
        return signals

    # Price at lower band + RSI oversold
    at_lower = df["close"] <= df["bb_lower"] * 1.005  # Small buffer
    rsi_oversold = df["rsi_14"] < 35

    # Price at upper band + RSI overbought
    at_upper = df["close"] >= df["bb_upper"] * 0.995
    rsi_overbought = df["rsi_14"] > 65

    signals[at_lower & rsi_oversold] = 1
    signals[at_upper & rsi_overbought] = -1

    return signals