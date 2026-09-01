"""Historical data ingestion from Binance REST API."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.data_collection.client import get_binance_client
from trading_bot.storage.database import get_database
from trading_bot.storage.models import Kline

logger = logging.getLogger(__name__)


class HistoricalDataIngester:
    """Fetch and store historical klines from Binance."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._client = get_binance_client()

    async def ingest_symbol(
        self,
        symbol: str,
        timeframe: str = "1h",
        days_back: int = 30,
        since: Optional[datetime] = None,
    ) -> int:
        """
        Ingest historical data for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            timeframe: Candle timeframe
            days_back: Days of history to fetch (ignored if since provided)
            since: Explicit start timestamp (UTC)

        Returns:
            Number of new candles inserted
        """
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=days_back)

        since_ms = int(since.timestamp() * 1000)
        logger.info(f"Starting historical ingestion for {symbol} {timeframe} from {since}")

        total_inserted = 0
        batch_size = 500  # Binance max limit
        current_since = since_ms

        while True:
            try:
                ohlcv = await self._client.fetch_ohlcv(
                    symbol=symbol,
                    timeframe=timeframe,
                    since=current_since,
                    limit=batch_size,
                )

                if not ohlcv:
                    logger.info(f"No more data for {symbol} {timeframe}")
                    break

                inserted = await self._store_klines(symbol, timeframe, ohlcv)
                total_inserted += inserted

                # If we got less than batch_size, we've reached the end
                if len(ohlcv) < batch_size:
                    break

                # Next batch starts after the last candle
                current_since = ohlcv[-1][0] + 1

                # Small delay to respect rate limits
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Error ingesting {symbol} {timeframe}: {e}")
                raise

        logger.info(f"Completed ingestion for {symbol} {timeframe}: {total_inserted} new candles")
        return total_inserted

    async def _store_klines(
        self, symbol: str, timeframe: str, ohlcv_data: list
    ) -> int:
        """Store klines in database, skipping duplicates."""
        inserted = 0

        for candle in ohlcv_data:
            ts, open_p, high, low, close, volume = candle[:6]
            open_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            close_time = open_time + self._timeframe_to_timedelta(timeframe)

            # Check if already exists
            stmt = select(Kline).where(
                Kline.symbol == symbol,
                Kline.timeframe == timeframe,
                Kline.open_time == open_time,
            )
            existing = await self._session.execute(stmt)
            if existing.scalar_one_or_none():
                continue

            kline = Kline(
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open_price=Decimal(str(open_p)),
                high_price=Decimal(str(high)),
                low_price=Decimal(str(low)),
                close_price=Decimal(str(close)),
                volume=Decimal(str(volume)),
                quote_volume=Decimal("0"),  # Will be filled if available
                trades_count=0,
                taker_buy_base_volume=Decimal("0"),
                taker_buy_quote_volume=Decimal("0"),
                is_closed=True,
            )
            self._session.add(kline)
            inserted += 1

        if inserted > 0:
            await self._session.flush()

        return inserted

    def _timeframe_to_timedelta(self, timeframe: str) -> timedelta:
        """Convert timeframe string to timedelta."""
        unit = timeframe[-1]
        value = int(timeframe[:-1])

        if unit == "m":
            return timedelta(minutes=value)
        elif unit == "h":
            return timedelta(hours=value)
        elif unit == "d":
            return timedelta(days=value)
        elif unit == "w":
            return timedelta(weeks=value)
        else:
            return timedelta(hours=1)


async def ingest_historical_data(
    symbols: list[str],
    timeframe: str = "1h",
    days_back: int = 30,
) -> dict[str, int]:
    """
    Convenience function to ingest historical data for multiple symbols.

    Returns:
        Dict mapping symbol to number of candles inserted
    """
    db = get_database()
    results = {}

    async with db.session() as session:
        ingester = HistoricalDataIngester(session)
        for symbol in symbols:
            try:
                count = await ingester.ingest_symbol(
                    symbol=symbol,
                    timeframe=timeframe,
                    days_back=days_back,
                )
                results[symbol] = count
            except Exception as e:
                logger.error(f"Failed to ingest {symbol}: {e}")
                results[symbol] = 0

    return results