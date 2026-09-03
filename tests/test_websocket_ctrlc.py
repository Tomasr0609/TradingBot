"""Tests para Bug A (REST polling reemplaza WebSocket) y Bug B (Ctrl+C Windows)."""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from trading_bot.storage.models import Base, Kline
import trading_bot.risk_management.models
import trading_bot.execution.models
from trading_bot.config.settings import get_settings

@pytest.fixture
async def test_db_ws():
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

@pytest.fixture
def mock_client_ws():
    mock = MagicMock()
    mock.initialize = AsyncMock()
    mock.close = AsyncMock()
    mock.watch_ohlcv = MagicMock()
    return mock

# ------------------------------------------------------------------
# Bug A - Aislamiento: KlineWebSocketListener sigue existiendo como clase (no integrada a bot)
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_isolated_websocket_listener_starts_and_stops(test_db_ws, mock_client_ws):
    """[Aislado] La clase KlineWebSocketListener (no usada por bot) inicia y se detiene correctamente."""
    from trading_bot.data_collection.websocket import KlineWebSocketListener
    async def fake_watch(symbol, timeframe):
        yield [int(datetime.now(timezone.utc).timestamp()*1000), 100, 110, 90, 105, 10, True]
        await asyncio.sleep(3600)
        if False:
            yield
    mock_client_ws.watch_ohlcv = fake_watch
    with patch("trading_bot.data_collection.websocket.get_binance_client", return_value=mock_client_ws):
        listener = KlineWebSocketListener(session_factory=test_db_ws.session_factory, symbols=["BTC/USDT"], timeframe="1h")
        await listener.start()
        assert listener._running is True
        assert len(listener._tasks) == 1
        await listener.stop()
        assert listener._running is False
        assert len(listener._tasks) == 0

@pytest.mark.asyncio
async def test_isolated_websocket_persists_candle(test_db_ws, mock_client_ws):
    """[Aislado] KlineWebSocketListener persiste vela correctamente (no es el flujo del bot actual)."""
    from trading_bot.data_collection.websocket import KlineWebSocketListener
    ts_ms = int(datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc).timestamp()*1000)
    ohlcv = [ts_ms, 50000, 51000, 49000, 50500, 123.45, True]
    listener = KlineWebSocketListener(session_factory=test_db_ws.session_factory, symbols=["BTC/USDT"], timeframe="1h")
    # Inyectar mock client
    listener._client = mock_client_ws
    await listener._process_kline("BTC/USDT", ohlcv)
    async with test_db_ws.session() as session:
        result = await session.execute(select(Kline).where(Kline.symbol=="BTC/USDT"))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert float(rows[0].close_price) == 50500

# ------------------------------------------------------------------
# Bug A - Nuevo flujo REST polling (reemplaza WebSocket)
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rest_polling_calls_ingest_with_since(test_db_ws):
    """En iteración del loop, se llama a ingest_symbol con since = última vela + 1 intervalo (no repite historial)."""
    from trading_bot.bot import TradingBot
    s = get_settings()
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    orig_symbols = s.trading_symbols
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    s.trading_symbols = ["BTC/USDT"]
    s.trading_timeframe = "1h"
    s.telegram_bot_token = ""
    # Preparar DB con una vela existente
    last_time = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    async with test_db_ws.session() as session:
        k = Kline(symbol="BTC/USDT", timeframe="1h", open_time=last_time, close_time=last_time+timedelta(hours=1), open_price=Decimal("100"), high_price=Decimal("110"), low_price=Decimal("90"), close_price=Decimal("105"), volume=Decimal("10"), quote_volume=Decimal("1000"), trades_count=10, taker_buy_base_volume=Decimal("5"), taker_buy_quote_volume=Decimal("500"), is_closed=True)
        session.add(k)
        await session.flush()
    # Mock ingester
    with patch("trading_bot.bot.get_binance_client") as mock_get_client, \
         patch("trading_bot.bot.get_database", return_value=test_db_ws), \
         patch("trading_bot.bot.init_db", new=AsyncMock()), \
         patch("trading_bot.data_collection.historical.HistoricalDataIngester") as MockIngester, \
         patch("trading_bot.bot.get_telegram_notifier") as mock_tele:
        mock_client = MagicMock()
        mock_client.initialize = AsyncMock()
        mock_client.close = AsyncMock()
        mock_get_client.return_value = mock_client
        mock_ingester = AsyncMock()
        mock_ingester.ingest_symbol = AsyncMock(return_value=1)
        MockIngester.return_value = mock_ingester
        mock_tele.return_value.configure = MagicMock()
        mock_tele.return_value.start = AsyncMock()
        mock_tele.return_value.stop = AsyncMock()
        with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_ws):
            bot = TradingBot()
            await bot.initialize()
            # Limpiar llamadas de initialize (cold start)
            mock_ingester.ingest_symbol.reset_mock()
            await bot._refresh_via_rest("BTC/USDT", "1h")
            # Debe haber sido llamado con since = última vela + 1 intervalo (no repite historial)
            assert mock_ingester.ingest_symbol.called, "ingest_symbol debería haber sido llamado"
            args, kwargs = mock_ingester.ingest_symbol.call_args
            called_since = kwargs.get("since") or (args[2] if len(args) > 2 else None) if len(args) > 2 else kwargs.get("since")
            # Si es llamado con since como kwarg, verificar
            if called_since is None:
                # Puede ser que se haya llamado con since como tercer positional
                called_since = kwargs.get("since")
            assert called_since is not None, "since no debe ser None cuando hay vela previa"
            # Normalizar timezone para comparar
            if called_since.tzinfo is None:
                called_since = called_since.replace(tzinfo=timezone.utc)
            expected_since = last_time + timedelta(hours=1)
            if expected_since.tzinfo is None:
                expected_since = expected_since.replace(tzinfo=timezone.utc)
            # Debe ser exactamente last + 1h (o muy cercano si hay conversión)
            assert abs((called_since - expected_since).total_seconds()) < 1, f"since {called_since} != expected {expected_since}"
            await bot.shutdown()
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode
    s.trading_symbols = orig_symbols
    import trading_bot.data_collection.client as mod
    mod._client_instance = None

@pytest.mark.asyncio
async def test_rest_polling_cold_start_uses_days_back(test_db_ws):
    """Si no hay velas guardadas (arranque en frío), usa fallback days_back."""
    from trading_bot.bot import TradingBot
    s = get_settings()
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    orig_symbols = s.trading_symbols
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    s.trading_symbols = ["BTC/USDT"]
    s.telegram_bot_token = ""
    with patch("trading_bot.bot.get_binance_client") as mock_get_client, \
         patch("trading_bot.bot.get_database", return_value=test_db_ws), \
         patch("trading_bot.bot.init_db", new=AsyncMock()), \
         patch("trading_bot.data_collection.historical.HistoricalDataIngester") as MockIngester, \
         patch("trading_bot.bot.get_telegram_notifier") as mock_tele:
        mock_client = MagicMock()
        mock_client.initialize = AsyncMock()
        mock_client.close = AsyncMock()
        mock_get_client.return_value = mock_client
        mock_ingester = AsyncMock()
        mock_ingester.ingest_symbol = AsyncMock(return_value=0)
        MockIngester.return_value = mock_ingester
        mock_tele.return_value.configure = MagicMock()
        mock_tele.return_value.start = AsyncMock()
        mock_tele.return_value.stop = AsyncMock()
        with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_ws):
            bot = TradingBot()
            await bot.initialize()
            mock_ingester.ingest_symbol.reset_mock()
            await bot._refresh_via_rest("BTC/USDT", "1h")
            # En frío debe llamar con days_back (since None) -> ingester decide based on days_back
            assert mock_ingester.ingest_symbol.called
            args, kwargs = mock_ingester.ingest_symbol.call_args
            # Debe tener days_back=7 (default de bot) y no since, o since None
            # Nuestra implementación llama con since=None y days_back=7 en frío
            assert kwargs.get("days_back") == 7 or "days_back" in str(mock_ingester.ingest_symbol.call_args)
            await bot.shutdown()
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode
    s.trading_symbols = orig_symbols
    import trading_bot.data_collection.client as mod
    mod._client_instance = None

@pytest.mark.asyncio
async def test_rest_polling_persisted_and_loadable(test_db_ws):
    """Tras iteración exitosa, _load_recent_candles incluye la vela nueva traída por REST."""
    from trading_bot.bot import TradingBot
    s = get_settings()
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    orig_symbols = s.trading_symbols
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    s.trading_symbols = ["BTC/USDT"]
    s.trading_timeframe = "1h"
    s.telegram_bot_token = ""
    # Preparar DB con vela vieja
    last_time = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    async with test_db_ws.session() as session:
        k = Kline(symbol="BTC/USDT", timeframe="1h", open_time=last_time, close_time=last_time+timedelta(hours=1), open_price=Decimal("100"), high_price=Decimal("110"), low_price=Decimal("90"), close_price=Decimal("105"), volume=Decimal("10"), quote_volume=Decimal("1000"), trades_count=10, taker_buy_base_volume=Decimal("5"), taker_buy_quote_volume=Decimal("500"), is_closed=True)
        session.add(k)
        await session.flush()
    # Mock ingester que inserta una nueva vela
    async def fake_ingest(symbol, timeframe, days_back=30, since=None):
        # Simula que Binance devuelve 1 vela nueva
        async with test_db_ws.session() as session:
            new_time = last_time + timedelta(hours=1)
            k2 = Kline(symbol=symbol, timeframe=timeframe, open_time=new_time, close_time=new_time+timedelta(hours=1), open_price=Decimal("105"), high_price=Decimal("115"), low_price=Decimal("95"), close_price=Decimal("110"), volume=Decimal("12"), quote_volume=Decimal("1200"), trades_count=10, taker_buy_base_volume=Decimal("6"), taker_buy_quote_volume=Decimal("600"), is_closed=True)
            session.add(k2)
            await session.flush()
        return 1

    with patch("trading_bot.bot.get_binance_client") as mock_get_client, \
         patch("trading_bot.bot.get_database", return_value=test_db_ws), \
         patch("trading_bot.bot.init_db", new=AsyncMock()), \
         patch("trading_bot.data_collection.historical.HistoricalDataIngester") as MockIngester, \
         patch("trading_bot.bot.get_telegram_notifier") as mock_tele:
        mock_client = MagicMock()
        mock_client.initialize = AsyncMock()
        mock_client.close = AsyncMock()
        mock_get_client.return_value = mock_client
        mock_ingester = MagicMock()
        mock_ingester.ingest_symbol = AsyncMock(side_effect=fake_ingest)
        MockIngester.return_value = mock_ingester
        mock_tele.return_value.configure = MagicMock()
        mock_tele.return_value.start = AsyncMock()
        mock_tele.return_value.stop = AsyncMock()
        with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_ws):
            bot = TradingBot()
            await bot.initialize()
            await bot._refresh_via_rest("BTC/USDT", "1h")
            df = await bot._load_recent_candles("BTC/USDT", "1h", limit=10)
            assert not df.empty
            assert len(df) == 2
            assert df["close"].iloc[-1] == 110
            await bot.shutdown()
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode
    s.trading_symbols = orig_symbols
    import trading_bot.data_collection.client as mod
    mod._client_instance = None

@pytest.mark.asyncio
async def test_rest_polling_error_does_not_crash_other_symbol(test_db_ws):
    """Error de red en polling de un símbolo no tumba el resto del loop ni al otro símbolo."""
    from trading_bot.bot import TradingBot
    s = get_settings()
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    orig_symbols = s.trading_symbols
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    s.trading_symbols = ["BTC/USDT", "FAIL/USDT"]
    s.telegram_bot_token = ""
    with patch("trading_bot.bot.get_binance_client") as mock_get_client, \
         patch("trading_bot.bot.get_database", return_value=test_db_ws), \
         patch("trading_bot.bot.init_db", new=AsyncMock()), \
         patch("trading_bot.data_collection.historical.HistoricalDataIngester") as MockIngester, \
         patch("trading_bot.bot.get_telegram_notifier") as mock_tele:
        mock_client = MagicMock()
        mock_client.initialize = AsyncMock()
        mock_client.close = AsyncMock()
        mock_get_client.return_value = mock_client
        # Configurar ingester: BTC ok, FAIL lanza excepción
        async def fake_ingest(symbol, timeframe, days_back=30, since=None):
            if symbol == "FAIL/USDT":
                raise RuntimeError("Simulated network failure")
            return 0
        mock_ingester = MagicMock()
        mock_ingester.ingest_symbol = AsyncMock(side_effect=fake_ingest)
        MockIngester.return_value = mock_ingester
        mock_tele.return_value.configure = MagicMock()
        mock_tele.return_value.start = AsyncMock()
        mock_tele.return_value.stop = AsyncMock()
        mock_tele.return_value.notify_risk_rejection = AsyncMock()
        with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_ws):
            bot = TradingBot()
            await bot.initialize()
            # Debe completar sin lanzar, a pesar de FAIL
            await bot._refresh_via_rest("BTC/USDT", "1h")
            await bot._refresh_via_rest("FAIL/USDT", "1h")
            # Verificar que run_once no crashea con FAIL
            # Mock _load_recent_candles para evitar que falle por falta de datos
            async def fake_load(symbol, timeframe, limit=200):
                if symbol == "FAIL/USDT":
                    raise RuntimeError("load fail")
                import pandas as pd, numpy as np
                dates = __import__("pandas").date_range("2023-01-01", periods=100, freq="1h", tz="UTC")
                np.random.seed(0)
                close = 100 + np.cumsum(np.random.normal(0,0.5,100))
                df = __import__("pandas").DataFrame({"open":close,"high":close+0.5,"low":close-0.5,"close":close,"volume":100}, index=dates)
                df.index.name="open_time"
                return df
            bot._load_recent_candles = fake_load
            # run_once debe procesar BTC a pesar de FAIL
            await bot.run_once()
            await bot.shutdown()
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode
    s.trading_symbols = orig_symbols
    import trading_bot.data_collection.client as mod
    mod._client_instance = None

# ------------------------------------------------------------------
# Bug B - Ctrl+C Windows
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bugB_ctrlc_calls_shutdown():
    """Simula KeyboardInterrupt durante stop.wait() y confirma que bot.shutdown() se llama sin traceback."""
    import importlib.util, pathlib
    from unittest.mock import AsyncMock, MagicMock, patch
    spec = importlib.util.spec_from_file_location("run_bot", pathlib.Path("scripts/run_bot.py"))
    run_bot = importlib.util.module_from_spec(spec)
    mock_bot = AsyncMock()
    mock_bot.initialize = AsyncMock()
    mock_bot.shutdown = AsyncMock()
    mock_bot.run_forever = AsyncMock()
    async def fake_run_forever():
        await asyncio.sleep(3600)
    mock_bot.run_forever.side_effect = fake_run_forever
    with patch("trading_bot.bot.TradingBot", return_value=mock_bot):
        spec.loader.exec_module(run_bot)
        async def fake_wait(self):
            raise KeyboardInterrupt("simulated Ctrl+C")
        with patch.object(asyncio.Event, "wait", fake_wait):
            with patch("asyncio.get_running_loop") as mock_loop:
                fake_loop = MagicMock()
                fake_loop.add_signal_handler.side_effect = NotImplementedError("Windows")
                mock_loop.return_value = fake_loop
                try:
                    await run_bot.main()
                except KeyboardInterrupt:
                    pytest.fail("main() propagó KeyboardInterrupt sin manejar")
                mock_bot.shutdown.assert_awaited_once()
                mock_bot.initialize.assert_awaited_once()
