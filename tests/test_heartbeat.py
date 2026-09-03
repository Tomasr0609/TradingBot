"""Tests para heartbeat visible en cada ciclo (no solo cuando hay señal)."""

import pytest
import logging
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pandas as pd
import numpy as np
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from trading_bot.storage.models import Base
from trading_bot.config.settings import get_settings

@pytest.fixture
async def test_db_hb():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    class FakeDB:
        def __init__(self, eng, fac): self.engine=eng; self.session_factory=fac
        def session(self):
            from contextlib import asynccontextmanager
            fac=self.session_factory
            @asynccontextmanager
            async def _cm():
                async with fac() as s:
                    try: yield s; await s.commit()
                    except: await s.rollback(); raise
            return _cm()
    db=FakeDB(engine, factory)
    yield db
    await engine.dispose()

def make_df_with_price(price=65000, regime="trend"):
    """Crea DataFrame minimal para que compute_indicators funcione y genere régimen."""
    dates = pd.date_range("2023-01-01", periods=100, freq="1h", tz="UTC")
    np.random.seed(0)
    # Crear datos que den régimen trend y precio cercano a price
    close = np.linspace(price-1000, price, 100) + np.random.normal(0, 10, 100)
    df = pd.DataFrame({"open": close, "high": close+5, "low": close-5, "close": close, "volume": 100}, index=dates)
    df.index.name = "open_time"
    return df

@pytest.mark.asyncio
async def test_heartbeat_sin_senal_emite_log_info(test_db_hb, caplog):
    """Con señal ninguna, igual se emite heartbeat INFO con precio y régimen."""
    from trading_bot.bot import TradingBot
    caplog.set_level(logging.INFO)
    s = get_settings()
    orig_symbols = s.trading_symbols
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    s.trading_symbols = ["BTC/USDT"]
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    s.telegram_bot_token = ""
    with patch("trading_bot.bot.get_binance_client") as mock_get_client, \
         patch("trading_bot.bot.get_database", return_value=test_db_hb), \
         patch("trading_bot.bot.init_db", new=AsyncMock()), \
         patch("trading_bot.bot.get_telegram_notifier") as mock_tele, \
         patch("trading_bot.risk_management.engine.get_database", return_value=test_db_hb), \
         patch("trading_bot.data_collection.websocket.KlineWebSocketListener") as MockWS:
        mock_ws = AsyncMock()
        mock_ws.start = AsyncMock()
        mock_ws.stop = AsyncMock()
        MockWS.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.initialize = AsyncMock()
        mock_client.close = AsyncMock()
        mock_get_client.return_value = mock_client
        mock_tele.return_value.configure = MagicMock()
        mock_tele.return_value.start = AsyncMock()
        mock_tele.return_value.stop = AsyncMock()
        bot = TradingBot()
        await bot.initialize()
        # Mockear _load_recent_candles para devolver df con precio conocido y sin señal
        df = make_df_with_price(price=64850.20)
        # Mockear estrategia que devuelve señal 0 siempre
        mock_strategy = MagicMock()
        mock_strategy.name = "test"
        # Señales todas 0
        signals = pd.Series(0, index=df.index)
        mock_strategy.generate_signals.return_value = signals
        # ATR y stop loss no necesarios para este camino (no llega a riesgo)
        with patch("trading_bot.bot.get_strategy", return_value=mock_strategy):
            # Patch compute_indicators y get_market_regime para controlar régimen y precio
            with patch("trading_bot.bot.compute_indicators") as mock_compute, \
                 patch("trading_bot.bot.get_market_regime", return_value="trend"):
                # compute_indicators devuelve df con close y atr
                df_with_ind = df.copy()
                df_with_ind["atr_14"] = 100
                df_with_ind["close"] = df["close"]
                mock_compute.return_value = df_with_ind
                # Mock _refresh_via_rest para no hacer network
                bot._refresh_via_rest = AsyncMock()
                bot._load_recent_candles = AsyncMock(return_value=df)
                # Mock strategy dentro de _process_symbol: ya está mockeado via get_strategy
                # Pero _process_symbol crea strategy via get_strategy, así que patchear ahí
                await bot._process_symbol("BTC/USDT", "1h", mock_strategy)
                # Verificar heartbeat
                # Caplog debe contener INFO con Ciclo OK, precio, régimen y señal=ninguna
                infos = [r for r in caplog.records if r.levelno == logging.INFO and "Ciclo OK" in r.message]
                assert len(infos) == 1, f"Esperaba 1 heartbeat, got {len(infos)}: {[r.message for r in infos]}"
                msg = infos[0].message
                assert "BTC/USDT" in msg
                assert "precio=" in msg
                assert "trend" in msg
                assert "ninguna" in msg
        await bot.shutdown()
    s.trading_symbols = orig_symbols
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode
    import trading_bot.data_collection.client as mod
    mod._client_instance = None

@pytest.mark.asyncio
async def test_heartbeat_con_senal_no_duplica_redundante(test_db_hb, caplog):
    """Con señal y orden ejecutada, no aparecen dos heartbeats redundantes (solo uno + Executed)."""
    from trading_bot.bot import TradingBot
    caplog.set_level(logging.INFO)
    s = get_settings()
    orig_symbols = s.trading_symbols
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    s.trading_symbols = ["BTC/USDT"]
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    s.telegram_bot_token = ""
    with patch("trading_bot.bot.get_binance_client") as mock_get_client, \
         patch("trading_bot.bot.get_database", return_value=test_db_hb), \
         patch("trading_bot.bot.init_db", new=AsyncMock()), \
         patch("trading_bot.bot.get_telegram_notifier") as mock_tele, \
         patch("trading_bot.risk_management.engine.get_database", return_value=test_db_hb), \
         patch("trading_bot.data_collection.websocket.KlineWebSocketListener") as MockWS:
        mock_ws = AsyncMock()
        mock_ws.start = AsyncMock()
        mock_ws.stop = AsyncMock()
        MockWS.return_value = mock_ws
        mock_client = MagicMock()
        mock_client.initialize = AsyncMock()
        mock_client.close = AsyncMock()
        mock_get_client.return_value = mock_client
        mock_tele.return_value.configure = MagicMock()
        mock_tele.return_value.start = AsyncMock()
        mock_tele.return_value.stop = AsyncMock()
        bot = TradingBot()
        await bot.initialize()
        # Preparar df y estrategia con señal BUY
        df = make_df_with_price(price=64850.20)
        mock_strategy = MagicMock()
        mock_strategy.name = "test"
        signals = pd.Series(0, index=df.index)
        signals.iloc[-1] = 1  # BUY en última
        mock_strategy.generate_signals.return_value = signals
        mock_strategy.get_stop_loss.return_value = 64000
        # Mock _load y compute
        df_with_ind = df.copy()
        df_with_ind["atr_14"] = 100
        df_with_ind["close"] = df["close"]
        # Mock executor para simular orden ejecutada
        mock_result = MagicMock()
        mock_result.executed = True
        mock_result.executed_size = Decimal("0.01")
        mock_result.executed_price = Decimal("64850.20")
        mock_result.stop_loss = Decimal("64000")
        mock_result.order_id = "test-123"
        mock_result.reason = "ok"
        bot.executor = AsyncMock()
        bot.executor.execute_via_risk = AsyncMock(return_value=mock_result)
        # Mock risk status para telegram
        bot.risk_engine.get_risk_status = AsyncMock(return_value={})
        bot._refresh_via_rest = AsyncMock()
        bot._load_recent_candles = AsyncMock(return_value=df)
        with patch("trading_bot.bot.compute_indicators", return_value=df_with_ind), \
             patch("trading_bot.bot.get_market_regime", return_value="trend"):
            caplog.clear()
            await bot._process_symbol("BTC/USDT", "1h", mock_strategy)
            # Filtrar logs heartbeat y executed
            heartbeats = [r for r in caplog.records if "Ciclo OK" in r.message]
            executeds = [r for r in caplog.records if "Executed" in r.message]
            # Debe haber exactamente 1 heartbeat con señal=BUY y 1 executed, no 2 heartbeats idénticos
            assert len(heartbeats) == 1, f"Esperaba 1 heartbeat con señal, got {len(heartbeats)}: {[r.message for r in heartbeats]}"
            assert "BUY" in heartbeats[0].message
            assert len(executeds) == 1
            # Verificar que no hay duplicación confusa: heartbeat y executed son distintos
            assert heartbeats[0].message != executeds[0].message
            # Verificar heartbeat contiene precio y régimen
            assert "precio=" in heartbeats[0].message
            assert "trend" in heartbeats[0].message
        await bot.shutdown()
    s.trading_symbols = orig_symbols
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode
    import trading_bot.data_collection.client as mod
    mod._client_instance = None
