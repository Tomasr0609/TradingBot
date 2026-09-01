"""WebSocket listener for real-time klines."""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.data_collection.client import get_binance_client
from trading_bot.storage.database import get_database
from trading_bot.storage.models import Kline

logger = logging.getLogger(__name__)


class KlineWebSocketListener:
    """Listen to real-time klines via Binance WebSocket and persist to DB."""

    def __init__(
        self,
        session_factory,
        symbols: list[str],
        timeframe: str = "1h",
        on_kline: Optional[Callable[[Kline], None]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._symbols = symbols
        self._timeframe = timeframe
        self._on_kline = on_kline
        self._client = get_binance_client()
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Start listening for all symbols."""
        if self._running:
            logger.warning("Listener already running")
            return

        self._running = True
        logger.info(f"Starting WebSocket listener for {self._symbols} @ {self._timeframe}")

        for symbol in self._symbols:
            task = asyncio.create_task(self._listen_symbol(symbol))
            self._tasks.append(task)

    async def stop(self) -> None:
        """Stop all listeners."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("WebSocket listener stopped")

    async def _listen_symbol(self, symbol: str) -> None:
        """Listen to klines for a single symbol."""
        backoff = 1

        while self._running:
            try:
                async for ohlcv in self._client.watch_ohlcv(symbol, self._timeframe):
                    if not self._running:
                        break

                    await self._process_kline(symbol, ohlcv)
                    backoff = 1  # Reset backoff on success

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket error for {symbol}: {e}")
                logger.info(f"Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # Exponential backoff, max 60s

    async def _process_kline(self, symbol: str, ohlcv: list) -> None:
        """Process and store a single kline."""
        ts, open_p, high, low, close, volume = ohlcv[:6]
        open_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        close_time = open_time + self._timeframe_to_timedelta(self._timeframe)
        is_closed = len(ohlcv) > 6 and ohlcv[6]  # Binance includes is_closed flag

        async with self._session_factory() as session:
            # Check for existing
            stmt = select(Kline).where(
                Kline.symbol == symbol,
                Kline.timeframe == self._timeframe,
                Kline.open_time == open_time,
            )
            result = await session.execute(stmt)
            kline = result.scalar_one_or_none()

            if kline:
                # Update existing
                kline.high_price = max(kline.high_price, Decimal(str(high)))
                kline.low_price = min(kline.low_price, Decimal(str(low)))
                kline.close_price = Decimal(str(close))
                kline.volume = Decimal(str(volume))
                kline.is_closed = is_closed
                kline.close_time = close_time
            else:
                # Create new
                kline = Kline(
                    symbol=symbol,
                    timeframe=self._timeframe,
                    open_time=open_time,
                    close_time=close_time,
                    open_price=Decimal(str(open_p)),
                    high_price=Decimal(str(high)),
                    low_price=Decimal(str(low)),
                    close_price=Decimal(str(close)),
                    volume=Decimal(str(volume)),
                    quote_volume=Decimal("0"),
                    trades_count=0,
                    taker_buy_base_volume=Decimal("0"),
                    taker_buy_quote_volume=Decimal("0"),
                    is_closed=is_closed,
                )
                session.add(kline)

            await session.flush()

            # Call callback if provided (for real-time processing)
            if self._on_kline and is_closed:
                self._on_kline(kline)

            logger.debug(f"Stored kline: {symbol} {self._timeframe} {open_time} closed={is_closed}")

    def _timeframe_to_timedelta(self, timeframe: str):
        """Convert timeframe string to timedelta."""
        from datetime import timedelta
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


async def run_websocket_listener(
    symbols: list[str],
    timeframe: str = "1h",
    on_kline: Optional[Callable[[Kline], None]] = None,
) -> KlineWebSocketListener:
    """
    Start a WebSocket listener for real-time klines.

    Returns:
        The listener instance (call .stop() to shut down)
    """
    db = get_database()
    listener = KlineWebSocketListener(
        session_factory=db.session_factory,
        symbols=symbols,
        timeframe=timeframe,
        on_kline=on_kline,
    )
    await listener.start()
    return listener