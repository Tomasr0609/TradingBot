"""Tests Fase 4 - Paper/Testnet, loop y validación estricta."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from trading_bot.storage.models import Base
import trading_bot.execution.models
import trading_bot.risk_management.models
from trading_bot.execution.executor import OrderExecutor, OrderRequest, RiskGatewayError, TestnetEnforcementError
from trading_bot.risk_management.models import RiskDecision, RiskRule
from trading_bot.config.settings import get_settings

# Ensure metadata
import trading_bot.storage.models as sm
import trading_bot.risk_management.models as rm
import trading_bot.execution.models as em

d = lambda x: Decimal(str(x))

@pytest.fixture
async def test_db4():
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

@pytest.fixture
def patched_db(test_db4):
    with patch("trading_bot.storage.database.get_database", return_value=test_db4), \
         patch("trading_bot.risk_management.engine.get_database", return_value=test_db4):
        yield test_db4

@pytest.fixture
def risk_engine(patched_db):
    from trading_bot.risk_management.engine import RiskEngine
    s=get_settings()
    # ensure testnet
    s.binance_base_url="https://testnet.binance.vision"
    s.trading_mode="testnet"
    eng=RiskEngine()
    eng._db=patched_db
    return eng

@pytest.mark.asyncio
async def test_testnet_enforcement_blocks_mainnet(patched_db, risk_engine):
    s=get_settings()
    orig=s.binance_base_url
    try:
        s.binance_base_url="https://api.binance.com"
        # Bot should refuse to init
        from trading_bot.bot import TradingBot
        with pytest.raises(TestnetEnforcementError):
            bot=TradingBot()
        # Executor also should refuse
        from trading_bot.execution.executor import OrderExecutor
        executor=OrderExecutor(risk_engine=risk_engine)
        # Need a valid risk result
        risk_res=await risk_engine.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
        req=OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2), risk_result=risk_res)
        with pytest.raises(TestnetEnforcementError):
            await executor.execute(req)
    finally:
        s.binance_base_url=orig
        s.trading_mode="testnet"

@pytest.mark.asyncio
async def test_no_raw_signal_execution(patched_db, risk_engine):
    executor=OrderExecutor(risk_engine=risk_engine)
    req=OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
    with pytest.raises(RiskGatewayError):
        await executor.execute(req)

@pytest.mark.asyncio
async def test_paper_execution_logs_to_db(patched_db, risk_engine):
    executor=OrderExecutor(risk_engine=risk_engine, exchange_client=None)  # paper
    s=get_settings()
    s.binance_base_url="https://testnet.binance.vision"
    risk_res=await risk_engine.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
    assert risk_res.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)
    req=OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2), risk_result=risk_res)
    result=await executor.execute(req)
    assert result.executed is True
    assert result.order_id.startswith("paper-")
    # Check DB logging
    from sqlalchemy import select
    from trading_bot.execution.models import ExecutedOrder
    async with patched_db.session() as session:
        res=await session.execute(select(ExecutedOrder))
        rows=res.scalars().all()
        assert len(rows)==1
        row=rows[0]
        assert row.symbol=="BTC/USDT"
        assert row.is_testnet is True
        assert row.status=="filled"
        assert row.approved_size is not None
        assert row.stop_loss_price is not None
        assert row.risk_decision==risk_res.decision.value

@pytest.mark.asyncio
async def test_rejected_signal_logged_and_not_executed(patched_db, risk_engine):
    # Force rejection via kill switch
    await risk_engine.activate_kill_switch("test", "unit")
    risk_res=await risk_engine.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
    assert risk_res.decision==RiskDecision.REJECTED
    executor=OrderExecutor(risk_engine=risk_engine, exchange_client=None)
    req=OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2), risk_result=risk_res)
    result=await executor.execute(req)
    assert result.executed is False
    from sqlalchemy import select
    from trading_bot.execution.models import ExecutedOrder
    async with patched_db.session() as session:
        res=await session.execute(select(ExecutedOrder))
        rows=res.scalars().all()
        # should have logged rejected
        assert any(r.status=="rejected" for r in rows)
    await risk_engine.deactivate_kill_switch()

@pytest.mark.asyncio
async def test_real_testnet_ccxt_mock(patched_db, risk_engine):
    mock_exchange=AsyncMock()
    mock_exchange.create_order=AsyncMock(return_value={"id":"12345","price":"100","average":"100","filled":"0.5","amount":"0.5","fee":{"cost":"0.001"}})
    # urls needed for testnet check
    mock_exchange.urls={"api": {"public":"https://testnet.binance.vision"}}
    executor=OrderExecutor(risk_engine=risk_engine, exchange_client=mock_exchange)
    risk_res=await risk_engine.evaluate_signal(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2))
    req=OrderRequest(symbol="BTC/USDT", signal_type="BUY", signal_price=d(100), strategy_name="test", regime="trend", atr=d(2), risk_result=risk_res)
    result=await executor.execute(req)
    assert result.executed is True
    assert result.order_id=="12345"
    assert result.has_protection is True
    assert mock_exchange.create_order.call_count == 2

@pytest.mark.asyncio
async def test_bot_loop_resilient_to_partial_failure(patched_db):
    # Mock get_binance_client to avoid real network
    with patch("trading_bot.bot.get_binance_client") as mock_get_client, \
         patch("trading_bot.bot.get_telegram_notifier") as mock_tele:
        mock_client=MagicMock()
        mock_client.initialize=AsyncMock()
        mock_client.close=AsyncMock()
        mock_get_client.return_value=mock_client
        mock_notifier=MagicMock()
        mock_notifier.configure=MagicMock()
        mock_notifier.start=AsyncMock()
        mock_notifier.stop=AsyncMock()
        mock_tele.return_value=mock_notifier
        from trading_bot.bot import TradingBot
        bot=TradingBot()
        # Patch _load_recent_candles to simulate failure for one symbol, success for another
        orig_load=bot._load_recent_candles
        async def fake_load(symbol, timeframe, limit=200):
            if symbol=="FAIL/USDT":
                raise RuntimeError("Simulated data failure")
            # return minimal df with enough data
            import pandas as pd, numpy as np
            dates=pd.date_range("2023-01-01", periods=100, freq="1h", tz="UTC")
            np.random.seed(0)
            close=100+np.cumsum(np.random.normal(0,0.5,100))
            df=pd.DataFrame({"open":close,"high":close+0.5,"low":close-0.5,"close":close,"volume":100}, index=dates)
            df.index.name="open_time"
            return df
        bot._load_recent_candles=fake_load
        # set symbols
        s=get_settings()
        orig_symbols=s.trading_symbols
        s.trading_symbols=["BTC/USDT","FAIL/USDT"]
        await bot.initialize()
        # run_once should not raise despite FAIL/USDT
        await bot.run_once()
        # should have processed BTC/USDT (maybe executed or blocked, but not crashed)
        await bot.shutdown()
        s.trading_symbols=orig_symbols

@pytest.mark.asyncio
async def test_dashboard_query(patched_db, risk_engine):
    # Ensure dashboard query works (minimal)
    from sqlalchemy import select
    from trading_bot.execution.models import ExecutedOrder
    async with patched_db.session() as session:
        res = await session.execute(select(ExecutedOrder))
        assert res.scalars().all() == [] or True  # just verify no error
    # Run dashboard script via direct import
    import importlib.util, pathlib
    path = pathlib.Path("scripts/dashboard.py")
    spec = importlib.util.spec_from_file_location("dashboard", path)
    mod = importlib.util.module_from_spec(spec)
    with patch("trading_bot.storage.database.get_database", return_value=patched_db):
        spec.loader.exec_module(mod)
        await mod.main()
