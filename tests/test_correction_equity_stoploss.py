"""Tests corrección equity continuo + stop loss real (pre-Testnet extendido)."""

from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from trading_bot.storage.models import Base
import trading_bot.execution.models, trading_bot.risk_management.models

d = lambda x: Decimal(str(x))

@pytest.fixture
async def test_db_corr():
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
    db=FakeDB(engine,factory)
    yield db
    await engine.dispose()

# ------------------------------------------------------------------
# Problema 1 - equity continuo
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_equity_continuity_starting_day2_equals_current_day1(test_db_corr):
    """starting_equity del día 2 == current_equity cierre día 1, no fijo 10000."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    from trading_bot.risk_management.models import DailyStats
    from sqlalchemy import select
    s=get_settings()
    s.binance_base_url="https://testnet.binance.vision"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_corr):
        eng=RiskEngine()
        eng._db=test_db_corr
        # Simular día 1: crear registro para ayer con current 9500
        yesterday = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        async with test_db_corr.session() as session:
            ds = DailyStats(date=yesterday, starting_equity=d(10000), current_equity=d(9500), peak_equity=d(10000), max_drawdown_pct=d(5), daily_loss_limit_pct=d(3), total_exposure=d(0), daily_pnl=d(-500), daily_pnl_pct=d(-5))
            session.add(ds)
            # También crear GlobalRiskState
            from trading_bot.risk_management.models import GlobalRiskState
            gs = GlobalRiskState(id=1, peak_equity=d(10000))
            session.add(gs)
            await session.flush()
        # Ahora crear día hoy via _get_or_create
        async with test_db_corr.session() as session:
            today = await eng._get_or_create_daily_stats(session)
            assert today.starting_equity == d(9500), f"Expected 9500 got {today.starting_equity}"
            assert today.current_equity == d(9500)
            # Peak debe ser continuo
            assert today.peak_equity == d(10000)
            assert today.max_drawdown_pct == d(5)

@pytest.mark.asyncio
async def test_fetch_balance_used_for_first_day(test_db_corr):
    """Al iniciar primer día, starting_equity viene de fetch_balance, no hardcode."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    s=get_settings()
    s.binance_base_url="https://testnet.binance.vision"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_corr):
        eng=RiskEngine()
        eng._db=test_db_corr
        # Mock fetch_balance to return 25000 USDT
        mock_client = AsyncMock()
        mock_client.fetch_balance = AsyncMock(return_value={"total": {"USDT": 25000}})
        with patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
            async with test_db_corr.session() as session:
                today = await eng._get_or_create_daily_stats(session)
                assert today.starting_equity == d(25000)
                assert today.current_equity == d(25000)

@pytest.mark.asyncio
async def test_drawdown_acumulado_3_dias_dispara_circuit_breaker(test_db_corr):
    """3 días con pérdidas parciales (cada una < límite diario) pero acumulado >15% debe disparar max drawdown."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.config.settings import get_settings
    s=get_settings()
    orig_drawdown=s.risk_max_drawdown_pct
    orig_daily=s.risk_max_daily_loss_pct
    s.risk_max_drawdown_pct=0.15
    s.risk_max_daily_loss_pct=0.20  # 20% diario para que acumulado 16% no dispare diario pero sí max drawdown
    s.binance_base_url="https://testnet.binance.vision"
    s.trading_mode="testnet"
    mock_client = AsyncMock()
    mock_client.fetch_balance = AsyncMock(return_value={"total": {"USDT": 10000}})
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_corr), \
         patch("trading_bot.data_collection.client.get_binance_client", return_value=mock_client):
        eng=RiskEngine()
        eng._db=test_db_corr
        # Día 1: pérdida 4% -> 9600, drawdown 4%
        async with test_db_corr.session() as session:
            await eng._get_or_create_daily_stats(session)
            await session.flush()
        await eng.update_daily_pnl(d(-400))
        # Simulamos 3 días con pérdidas que individualmente <20% pero acumulado 16% >15%
        # Como estamos en mismo DailyStats, daily_pnl acumulado sería -1600 (16%) que con límite 20% no disparará diario
        await eng.update_daily_pnl(d(-500))  # 9100
        await eng.update_daily_pnl(d(-700))  # 8400
        # Ahora debe disparar max_drawdown (global 16%)
        res = await eng.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
        from trading_bot.risk_management.models import RiskDecision, RiskRule
        assert res.decision == RiskDecision.REJECTED
        assert res.rule == RiskRule.MAX_DRAWDOWN
        # Verificar equity continuo: starting del día 2 debe ser prev current
        # Lo verificamos con test separado que ya pasa, aquí solo drawdown
    s.risk_max_daily_loss_pct=orig_daily
    s.risk_max_drawdown_pct=orig_drawdown

# ------------------------------------------------------------------
# Problema 2 - stop loss real
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stop_loss_colocado_tras_orden_entrada(test_db_corr):
    """Toda orden de entrada exitosa resulta en intento de colocación de stop loss."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.execution.executor import OrderExecutor, OrderRequest
    from trading_bot.config.settings import get_settings
    from sqlalchemy import select
    from trading_bot.execution.models import ExecutedOrder
    s=get_settings()
    s.binance_base_url="https://testnet.binance.vision"
    s.trading_mode="testnet"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_corr), \
         patch("trading_bot.storage.database.get_database", return_value=test_db_corr):
        eng=RiskEngine()
        eng._db=test_db_corr
        # Mock exchange con dos órdenes: entry y sl
        mock_exchange = AsyncMock()
        mock_exchange.urls = {"api": {"public": "https://testnet.binance.vision"}}
        # entry order
        mock_exchange.create_order = AsyncMock(side_effect=[
            {"id": "entry-123", "price": "100", "average": "100", "filled": "0.5", "amount": "0.5", "fee": {"cost": "0.001"}},
            {"id": "sl-456", "price": "95", "average": "95", "filled": "0.0", "amount": "0.5"},
        ])
        executor = OrderExecutor(risk_engine=eng, exchange_client=mock_exchange)
        risk_res = await eng.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
        req = OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2), risk_result=risk_res)
        result = await executor.execute(req)
        assert result.executed is True
        assert result.stop_loss_order_id == "sl-456"
        assert result.has_protection is True
        assert mock_exchange.create_order.call_count == 2
        # Verifica DB guarda stop_loss_order_id y has_protection
        async with test_db_corr.session() as session:
            res = await session.execute(select(ExecutedOrder).order_by(ExecutedOrder.id.desc()).limit(1))
            row = res.scalar_one()
            assert row.stop_loss_order_id == "sl-456"
            assert row.has_protection is True
            assert row.status == "filled"

@pytest.mark.asyncio
async def test_stop_loss_fallo_notifica_unprotected(test_db_corr):
    """Si colocación de stop falla, notifica Telegram y registra sin protección."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.execution.executor import OrderExecutor, OrderRequest
    from trading_bot.config.settings import get_settings
    from sqlalchemy import select
    from trading_bot.execution.models import ExecutedOrder
    s=get_settings()
    s.binance_base_url="https://testnet.binance.vision"
    s.trading_mode="testnet"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_corr), \
         patch("trading_bot.storage.database.get_database", return_value=test_db_corr):
        eng=RiskEngine()
        eng._db=test_db_corr
        mock_exchange = AsyncMock()
        mock_exchange.urls = {"api": {"public": "https://testnet.binance.vision"}}
        # Entry succeeds, stop fails twice (stop_loss_limit y fallback STOP_LOSS)
        mock_exchange.create_order = AsyncMock(side_effect=[
            {"id": "entry-789", "price": "100", "average": "100", "filled": "0.5", "amount": "0.5", "fee": {"cost": "0.001"}},
            Exception("Stop loss rejected by exchange"),
            Exception("Stop loss rejected by exchange"),
        ])
        mock_telegram = AsyncMock()
        mock_telegram.notify_unprotected_position = AsyncMock()
        executor = OrderExecutor(risk_engine=eng, exchange_client=mock_exchange, telegram_notifier=mock_telegram)
        risk_res = await eng.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
        req = OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2), risk_result=risk_res)
        result = await executor.execute(req)
        assert result.executed is True  # entrada sí se ejecutó
        assert result.has_protection is False
        assert result.stop_loss_order_id is None
        # Notificación llamada
        mock_telegram.notify_unprotected_position.assert_awaited_once()
        args = mock_telegram.notify_unprotected_position.await_args[0]
        assert "BTC/USDT" in args[0]
        # DB registra unprotected
        async with test_db_corr.session() as session:
            res = await session.execute(select(ExecutedOrder).order_by(ExecutedOrder.id.desc()).limit(1))
            row = res.scalar_one()
            assert row.has_protection is False
            assert row.status == "unprotected"
            assert "Stop loss rejected" in (row.error_message or "")
            assert row.stop_loss_order_id is None

@pytest.mark.asyncio
async def test_paper_mode_simula_stop_protection(test_db_corr):
    """En paper mode también se simula stop con has_protection True."""
    from trading_bot.risk_management.engine import RiskEngine
    from trading_bot.execution.executor import OrderExecutor, OrderRequest
    from trading_bot.config.settings import get_settings
    from sqlalchemy import select
    from trading_bot.execution.models import ExecutedOrder
    s=get_settings()
    s.binance_base_url="https://testnet.binance.vision"
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db_corr), \
         patch("trading_bot.storage.database.get_database", return_value=test_db_corr):
        eng=RiskEngine()
        eng._db=test_db_corr
        executor = OrderExecutor(risk_engine=eng, exchange_client=None)
        risk_res = await eng.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
        req = OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2), risk_result=risk_res)
        result = await executor.execute(req)
        assert result.has_protection is True
        assert result.stop_loss_order_id.startswith("paper-sl-")
        async with test_db_corr.session() as session:
            res = await session.execute(select(ExecutedOrder).order_by(ExecutedOrder.id.desc()).limit(1))
            row = res.scalar_one()
            assert row.has_protection is True
            assert row.stop_loss_order_id is not None
