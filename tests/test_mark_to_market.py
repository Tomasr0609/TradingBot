"""Tests para corrección mark-to-market equity (no solo USDT)."""

from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from trading_bot.storage.models import Base

d = lambda x: Decimal(str(x))

@pytest.fixture
async def test_db_mtm():
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

@pytest.mark.asyncio
async def test_equity_incluye_posiciones_abiertas(test_db_mtm):
    """Balance USDT 8000 + ETH 0.8 * 2500 = 10000, no solo 8000."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    s = get_settings()
    s.binance_base_url = "https://testnet.binance.vision"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_mtm):
        eng = RiskEngine()
        eng._db = test_db_mtm
        # Mock balance con ETH
        mock_client = AsyncMock()
        mock_client.fetch_balance = AsyncMock(return_value={
            "USDT": {"total": 8000, "free": 8000, "used": 0},
            "ETH": {"total": 0.8, "free": 0.8, "used": 0},
            "total": {"USDT": 8000, "ETH": 0.8}
        })
        # Mock ticker ETH/USDT = 2500
        mock_client.fetch_ticker = AsyncMock(return_value={"last": 2500, "close": 2500})
        with patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
            equity = await eng._fetch_initial_equity()
            # Antes del fix hubiera dado 8000, ahora debe dar 10000
            assert equity == d(10000), f"Equity {equity} != 10000 (8000 + 0.8*2500)"
            # Verificar que no es solo USDT
            assert equity != d(8000)
            # Verificar que fetch_ticker fue llamado para ETH
            mock_client.fetch_ticker.assert_awaited()

@pytest.mark.asyncio
async def test_drawdown_no_falso_positivo_con_posicion(test_db_mtm):
    """Con posición abierta pero sin pérdida real, drawdown no debe disparar."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    from trading_bot.risk_management.models import RiskDecision
    s = get_settings()
    orig_dd = s.risk_max_drawdown_pct
    s.risk_max_drawdown_pct = 0.15
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    # Balance con ETH, equity total 10000, sin pérdida
    mock_balance = {
        "USDT": {"total": 8000},
        "ETH": {"total": 0.8},
        "total": {"USDT": 8000, "ETH": 0.8}
    }
    mock_client = AsyncMock()
    mock_client.fetch_balance = AsyncMock(return_value=mock_balance)
    mock_client.fetch_ticker = AsyncMock(return_value={"last": 2500})
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_mtm), \
         patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
        eng = RiskEngine()
        eng._db = test_db_mtm
        # Inicializa equity correctamente
        async with test_db_mtm.session() as session:
            ds = await eng._get_or_create_daily_stats(session)
            assert ds.starting_equity == d(10000)
            assert ds.current_equity == d(10000)
        # Intenta evaluar señal - no debe ser bloqueado por drawdown
        res = await eng.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
        # Si equity fuera solo 8000, drawdown sería (10000-8000)/10000=20% -> bloquearía
        # Con fix, equity 10000 -> drawdown 0% -> no bloquea
        assert res.decision != RiskDecision.REJECTED or res.rule.value != "max_drawdown", f"False positive max_drawdown with equity {res.reason}"
        # Forzar que no es max_drawdown
        if res.decision == RiskDecision.REJECTED:
            assert res.rule.value != "max_drawdown"
    s.risk_max_drawdown_pct = orig_dd

@pytest.mark.asyncio
async def test_fetch_balance_no_redundante_en_inicializacion(test_db_mtm):
    """fetch_balance no se llama más de una vez de forma redundante durante misma inicialización."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    s = get_settings()
    s.binance_base_url = "https://testnet.binance.vision"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_mtm):
        eng = RiskEngine()
        eng._db = test_db_mtm
        mock_client = AsyncMock()
        # Contador
        call_count = {"n": 0}
        async def fake_fetch_balance():
            call_count["n"] += 1
            return {"total": {"USDT": 10000}, "USDT": {"total": 10000}}
        mock_client.fetch_balance = fake_fetch_balance
        mock_client.fetch_ticker = AsyncMock(return_value={"last": 100})
        with patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
            # Llama a _get_or_create_daily_stats y luego _get_global_state en misma "inicialización"
            async with test_db_mtm.session() as session:
                await eng._get_or_create_daily_stats(session)
                await eng._get_global_state(session)
                # _fetch_initial_equity debería haber sido llamado solo 1 vez, no 2, gracias a cache de 5s
                assert call_count["n"] == 1, f"fetch_balance llamado {call_count['n']} veces, esperado 1 (cache)"

@pytest.mark.asyncio
async def test_fallback_si_precio_no_disponible(test_db_mtm, caplog):
    """Si no se puede obtener precio de un activo gestionado puntual, continúa con resto y loguea."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    s = get_settings()
    # Asegurar que BTC está en symbols_list (gestionado) para que se intente valuar
    orig_symbols = s.trading_symbols
    s.trading_symbols = ["BTC/USDT", "ETH/USDT"]
    s.binance_base_url = "https://testnet.binance.vision"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_mtm):
        eng = RiskEngine()
        eng._db = test_db_mtm
        # BTC es gestionado pero su precio fallará; ETH ok; XYZ es dust no gestionado y debe ignorarse
        mock_client = AsyncMock()
        mock_client.fetch_balance = AsyncMock(return_value={
            "USDT": {"total": 5000},
            "BTC": {"total": 0.5},  # gestionado, fallará
            "ETH": {"total": 1},
            "XYZ": {"total": 10},  # dust no gestionado
        })
        async def fake_ticker(symbol):
            if symbol == "BTC/USDT":
                raise Exception("Symbol not found")
            if symbol == "ETH/USDT":
                return {"last": 2000}
            raise Exception("unknown")
        mock_client.fetch_ticker = AsyncMock(side_effect=fake_ticker)
        with patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
            equity = await eng._fetch_initial_equity()
            # Debe sumar USDT 5000 + ETH 1*2000 = 7000, ignorando BTC (falló) y XYZ (dust no gestionado)
            assert equity == d(7000), f"Equity {equity} should be 7000 (5000 + 2000), BTC fallo y XYZ ignorado"
            assert any("BTC" in rec.message for rec in caplog.records) or any("No se pudo valuar" in rec.message for rec in caplog.records)
            assert equity != d(5000)
    s.trading_symbols = orig_symbols

@pytest.mark.asyncio
async def test_equity_solo_activos_gestionados_con_20_dust(test_db_mtm):
    """
    Test obligatorio: balance con USDT + 2 símbolos gestionados (BTC,ETH) + 20 dust irrelevantes.
    Debe sumar solo USDT+BTC+ETH, y fetch_ticker max 2 veces.
    Comportamiento intencional: el bot valúa su equity gestionado, no el patrimonio total de fábrica del faucet.
    """
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    s = get_settings()
    orig_symbols = s.trading_symbols
    s.trading_symbols = ["BTC/USDT", "ETH/USDT"]
    s.binance_base_url = "https://testnet.binance.vision"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_mtm):
        eng = RiskEngine()
        eng._db = test_db_mtm
        # Construir balance con 20 dust
        dust_assets = ["BNB","LTC","TRX","XRP","ADA","DOT","LINK","BCH","XLM","ETC","FIL","VET","EOS","TRX","XMR","ZEC","DASH","XTZ","ATOM","NEO"]
        balance = {
            "USDT": {"total": 5000},
            "BTC": {"total": 0.1},
            "ETH": {"total": 2},
        }
        for asset in dust_assets:
            balance[asset] = {"total": 10}
            balance["total"] = balance.get("total", {})
        # Añadir total dict para ccxt
        total_dict = {"USDT": 5000, "BTC": 0.1, "ETH": 2}
        for a in dust_assets:
            total_dict[a] = 10
        balance["total"] = total_dict

        mock_client = AsyncMock()
        mock_client.fetch_balance = AsyncMock(return_value=balance)
        # Mock ticker solo para BTC y ETH
        async def fake_ticker(symbol):
            if symbol == "BTC/USDT":
                return {"last": 30000}
            if symbol == "ETH/USDT":
                return {"last": 2000}
            # Si se llama para dust, sería bug - hacemos que falle fuerte
            raise AssertionError(f"fetch_ticker no debería ser llamado para dust {symbol}")
        mock_client.fetch_ticker = AsyncMock(side_effect=fake_ticker)
        with patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
            equity = await eng._fetch_initial_equity()
            # Equity gestionado: 5000 + 0.1*30000=3000 + 2*2000=4000 => 12000
            # Dust (20*10 con precios inventados) debe ser ignorado
            assert equity == d(12000), f"Equity {equity} debe ser 5000+3000+4000=12000, dust ignorado"
            # Intencional: solo gestionados
            assert equity != d(5000 + 3000 + 4000 + 20*10*1000), "No debe sumar dust"
            # fetch_ticker máximo 2 veces (BTC y ETH)
            assert mock_client.fetch_ticker.call_count <= 2, f"fetch_ticker llamado {mock_client.fetch_ticker.call_count} veces, esperado <=2"
            assert mock_client.fetch_ticker.call_count == 2
            # Comentario explícito: comportamiento intencional
            assert True, "El bot valúa su propio equity gestionado (symbols_list), no el patrimonio total de fábrica del faucet"
    s.trading_symbols = orig_symbols
