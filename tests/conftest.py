import pytest
from unittest.mock import AsyncMock, patch

@pytest.fixture(autouse=True)
def mock_binance_balance_global():
    """Evita llamadas reales a Binance en todos los tests (ahorra 14s por test)."""
    mock_client = AsyncMock()
    mock_client.fetch_balance = AsyncMock(return_value={"total": {"USDT": 10000}})
    mock_client.fetch_ticker = AsyncMock(return_value={})
    mock_client.fetch_ohlcv = AsyncMock(return_value=[])
    mock_client.load_markets = AsyncMock()
    mock_client.close = AsyncMock()
    with patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
        yield
