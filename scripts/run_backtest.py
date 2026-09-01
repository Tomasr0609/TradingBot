#!/usr/bin/env python
"""Script to run backtests on historical data."""

import asyncio
import logging
import sys

import pandas as pd

from trading_bot.backtesting import run_backtest, print_backtest_report
from trading_bot.config.settings import get_settings
from trading_bot.decision import get_strategy
from trading_bot.storage.database import get_database, init_db
from trading_bot.storage.models import Kline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def load_klines_from_db(symbol: str, timeframe: str, limit: int = 5000) -> pd.DataFrame:
    """Load klines from database into DataFrame."""
    db = get_database()

    async with db.session() as session:
        from sqlalchemy import select
        stmt = (
            select(Kline)
            .where(
                Kline.symbol == symbol,
                Kline.timeframe == timeframe,
                Kline.is_closed == True,
            )
            .order_by(Kline.open_time.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        klines = result.scalars().all()

    if not klines:
        logger.warning(f"No data found for {symbol} {timeframe}")
        return pd.DataFrame()

    # Convert to DataFrame
    data = []
    for k in reversed(klines):  # Reverse to get chronological order
        data.append({
            "open_time": k.open_time,
            "open": float(k.open_price),
            "high": float(k.high_price),
            "low": float(k.low_price),
            "close": float(k.close_price),
            "volume": float(k.volume),
        })

    df = pd.DataFrame(data)
    df.set_index("open_time", inplace=True)
    df.index = pd.to_datetime(df.index, utc=True)

    logger.info(f"Loaded {len(df)} candles for {symbol} {timeframe}")
    return df


async def run_backtests() -> None:
    """Run backtests for all configured symbols and strategies."""
    settings = get_settings()

    # Initialize database
    await init_db()

    strategies_to_test = [
        "sma_crossover",
        "ema_macd",
        "bollinger_mr",
        "composite",
    ]

    for symbol in settings.symbols_list:
        logger.info(f"\n{'='*60}")
        logger.info(f"BACKTESTING {symbol} {settings.trading_timeframe}")
        logger.info(f"{'='*60}")

        df = await load_klines_from_db(symbol, settings.trading_timeframe, limit=5000)

        if df.empty or len(df) < 100:
            logger.warning(f"Insufficient data for {symbol}")
            continue

        logger.info(f"Data range: {df.index[0]} to {df.index[-1]} ({len(df)} candles)")

        for strat_name in strategies_to_test:
            try:
                strategy = get_strategy(strat_name)
                logger.info(f"\nRunning {strat_name}...")

                result = run_backtest(
                    df=df,
                    strategy=strategy,
                    symbol=symbol,
                    timeframe=settings.trading_timeframe,
                    initial_capital=10000,
                    fees=0.001,
                    slippage=0.0005,
                )

                print_backtest_report(result)

            except Exception as e:
                logger.error(f"Error running {strat_name}: {e}")
                import traceback
                traceback.print_exc()


async def main() -> None:
    try:
        await run_backtests()
    except Exception as e:
        logger.error(f"Backtest failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())