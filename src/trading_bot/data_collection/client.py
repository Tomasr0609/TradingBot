"""Binance exchange client using ccxt."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import ccxt.async_support as ccxt
from ccxt.base.errors import (
    AuthenticationError,
    BadSymbol,
    ExchangeError,
    NetworkError,
    RateLimitExceeded,
)

from trading_bot.config.settings import get_settings

logger = logging.getLogger(__name__)


class BinanceClient:
    """Async wrapper around ccxt for Binance Testnet."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._exchange: Optional[ccxt.binance] = None
        self._initialized = False

    async def _ensure_initialized(self) -> ccxt.binance:
        """Lazy initialization of the exchange."""
        if not self._initialized:
            await self.initialize()
        assert self._exchange is not None
        return self._exchange

    async def initialize(self) -> None:
        """Initialize the ccxt exchange instance."""
        if self._initialized:
            return

        # Verify we're using testnet
        if not self._settings.is_testnet:
            raise RuntimeError(
                f"Refusing to connect to non-testnet URL: {self._settings.binance_base_url}"
            )

        self._exchange = ccxt.binance(
            {
                "apiKey": self._settings.binance_api_key,
                "secret": self._settings.binance_api_secret,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                    "fetchCurrencies": False,
                },
                "enableRateLimit": True,
                "rateLimit": 1200,  # ms between requests
            }
        )
        # Testnet: usar sandbox mode de ccxt (arma todas las URLs correctamente con /api/v3 incluido)
        # Solo en Testnet, nunca en live (validación Fase 6 bloquea live sin autorización)
        if self._settings.is_testnet:
            self._exchange.set_sandbox_mode(True)

        # Load markets to validate symbols
        await self._exchange.load_markets()
        self._initialized = True
        logger.info("Binance Testnet client initialized")

    async def close(self) -> None:
        """Close the exchange connection."""
        if self._exchange:
            await self._exchange.close()
            self._exchange = None
            self._initialized = False

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[ccxt.binance, None]:
        """Context manager for exchange connection."""
        exchange = await self._ensure_initialized()
        try:
            yield exchange
        except Exception as e:
            logger.error(f"Exchange error: {e}")
            raise

    # --- REST API Methods ---

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 500,
    ) -> list:
        """
        Fetch historical OHLCV candles.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            timeframe: Candle timeframe (e.g., "1h", "4h", "1d")
            since: Timestamp in milliseconds (optional)
            limit: Number of candles (max 1000 for Binance)

        Returns:
            List of [timestamp, open, high, low, close, volume]
        """
        exchange = await self._ensure_initialized()

        # Validate symbol exists
        if symbol not in exchange.markets:
            raise BadSymbol(f"Symbol {symbol} not found on exchange")

        try:
            ohlcv = await exchange.fetch_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                since=since,
                limit=limit,
            )
            logger.debug(f"Fetched {len(ohlcv)} candles for {symbol} {timeframe}")
            return ohlcv
        except RateLimitExceeded:
            logger.warning("Rate limit exceeded, backing off")
            await asyncio.sleep(60)
            raise
        except NetworkError as e:
            logger.error(f"Network error fetching OHLCV: {e}")
            raise
        except ExchangeError as e:
            logger.error(f"Exchange error fetching OHLCV: {e}")
            raise

    async def fetch_ticker(self, symbol: str) -> dict:
        """Fetch current ticker for a symbol."""
        exchange = await self._ensure_initialized()
        return await exchange.fetch_ticker(symbol)

    async def fetch_balance(self) -> dict:
        """Fetch account balance (requires API key with permissions)."""
        exchange = await self._ensure_initialized()
        return await exchange.fetch_balance()

    # --- WebSocket Methods ---

    async def watch_ohlcv(
        self, symbol: str, timeframe: str = "1h"
    ) -> AsyncGenerator[list, None]:
        """
        Watch OHLCV candles via WebSocket.

        Yields:
            List: [timestamp, open, high, low, close, volume]
        """
        exchange = await self._ensure_initialized()

        while True:
            try:
                ohlcv = await exchange.watch_ohlcv(symbol, timeframe)
                yield ohlcv
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"WebSocket error for {symbol} {timeframe}: {e}")
                # Exponential backoff
                await asyncio.sleep(5)
                continue


# Singleton instance
_client_instance: Optional[BinanceClient] = None


def get_binance_client() -> BinanceClient:
    """Get singleton Binance client instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = BinanceClient()
    return _client_instance