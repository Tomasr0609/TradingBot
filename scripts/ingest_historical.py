#!/usr/bin/env python
"""Script to ingest historical data from Binance Testnet."""

import asyncio
import logging
import sys

from trading_bot.config.settings import get_settings
from trading_bot.data_collection import ingest_historical_data
from trading_bot.storage.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()

    # Verify testnet
    if not settings.is_testnet:
        logger.error(f"NOT TESTNET! Base URL: {settings.binance_base_url}")
        sys.exit(1)

    logger.info(f"Using Binance Testnet: {settings.binance_base_url}")
    logger.info(f"Symbols: {settings.symbols_list}")
    logger.info(f"Timeframe: {settings.trading_timeframe}")

    # Initialize database
    await init_db()

    # Ingest historical data
    results = await ingest_historical_data(
        symbols=settings.symbols_list,
        timeframe=settings.trading_timeframe,
        days_back=30,
    )

    logger.info("Ingestion results:")
    for symbol, count in results.items():
        logger.info(f"  {symbol}: {count} new candles")


if __name__ == "__main__":
    asyncio.run(main())