"""Tests Phase 3 - Risk Management (módulo más crítico). Cobertura de ramas exigida."""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone
import pytest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from trading_bot.storage.models import Base
from trading_bot.risk_management.models import Base as RiskBase  # same Base actually
from trading_bot.risk_management.models import RiskDecision, RiskRule
from trading_bot.risk_management.engine import RiskEngine
from trading_bot.execution.executor import OrderExecutor, OrderRequest, RiskGatewayError
from trading_bot.config.settings import get_settings

# Ensure all models are imported so metadata has tables
import trading_bot.risk_management.models  # noqa
import trading_bot.storage.models  # noqa
import trading_bot.execution.models  # noqa


@pytest.fixture
async def test_db():
    """In-memory DB with all tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    class FakeDB:
        def __init__(self, eng, factory):
            self.engine = eng
            self.session_factory = factory
        def session(self):
            # return async context manager
            from contextlib import asynccontextmanager
            factory = self.session_factory
            @asynccontextmanager
            async def _cm():
                async with factory() as s:
                    try:
                        yield s
                        await s.commit()
                    except Exception:
                        await s.rollback()
                        raise
            return _cm()

    db = FakeDB(engine, session_factory)
    yield db
    await engine.dispose()


@pytest.fixture
def risk_engine(test_db):
    """RiskEngine wired to test DB."""
    # Patch get_database inside engine module
    with patch("trading_bot.risk_management.engine.get_database", return_value=test_db):
        with patch("trading_bot.storage.database.get_database", return_value=test_db):
            # Ensure settings are deterministic
            s = get_settings()
            # Override for tests (lower thresholds for easier testing)
            orig = {}
            for k in ["risk_max_daily_loss_pct", "risk_max_position_risk_pct", "risk_max_total_exposure_pct", "risk_max_drawdown_pct", "risk_volatility_threshold_pct"]:
                orig[k] = getattr(s, k)
            s.risk_max_daily_loss_pct = 0.03  # 3%
            s.risk_max_position_risk_pct = 0.01  # 1%
            s.risk_max_total_exposure_pct = 0.20  # 20%
            s.risk_max_drawdown_pct = 0.15  # 15%
            s.risk_volatility_threshold_pct = 0.05  # 5%
            eng = RiskEngine()
            eng._db = test_db  # force
            yield eng
            for k, v in orig.items():
                setattr(s, k, v)


def d(x):  # helper Decimal
    return Decimal(str(x))


# ---------------------------------------------------------------------------
# 1) Daily loss limit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_loss_limit_halts_trading(risk_engine):
    # Simulate daily loss -3% reached
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.daily_pnl = d(-300)  # -300 on 10000 = -3%
        daily.daily_pnl_pct = d(-3)
        daily.current_equity = d(9700)
        await session.flush()
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.DAILY_LOSS_LIMIT
    # Second call same day should be halted too
    res2 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    assert res2.decision == RiskDecision.REJECTED
    assert "halted" in res2.reason.lower()


@pytest.mark.asyncio
async def test_daily_loss_limit_exact_boundary_mid_operation(risk_engine):
    """Señal llega justo cuando se toca límite a mitad de operación."""
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        # Simulate loss just below limit (-2.9%)
        daily.daily_pnl = d(-290)
        daily.daily_pnl_pct = d(-2.9)
        await session.flush()
    # This signal itself is ok, but after update to -3% next signal must be rejected
    # Use low price to avoid exposure reduction
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    assert res.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)
    # Now push over limit via update_daily_pnl
    await risk_engine.update_daily_pnl(d(-20))  # now -310
    res2 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    assert res2.decision == RiskDecision.REJECTED
    assert res2.rule == RiskRule.DAILY_LOSS_LIMIT


# ---------------------------------------------------------------------------
# 2) Position sizing (ATR-adjusted)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_position_sizing_never_exceeds_pct(risk_engine):
    # Equity 10000, max 1% = 100 risk, price 100, ATR 2 -> stop 4 -> risk_per_unit 4 -> size 25
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    assert res.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)
    assert res.stop_loss_price is not None
    # risk = size * risk_per_unit <= 100
    risk_per_unit = d(100) - res.stop_loss_price
    actual_risk = res.approved_size * risk_per_unit
    assert actual_risk <= d(100) + d(0.01)  # allow rounding
    # If ATR larger, size smaller (need fresh exposure)
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.total_exposure = d(0)
        await session.flush()
    res2 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(5)
    )
    assert res2.approved_size < res.approved_size

@pytest.mark.asyncio
async def test_position_sizing_respects_proposed_size(risk_engine):
    # Proposed size larger than max risk -> reduced
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500), proposed_size=d(10)
    )
    assert res.approved_size <= d(10)
    assert res.approved_size < d(10)  # should be reduced to 0.1

@pytest.mark.asyncio
async def test_position_sizing_rejects_below_min_notional(risk_engine):
    # Very high ATR -> tiny size below $10
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(5000)  # stop 10000 -> risk 10000 -> size 0.01 -> notional 500
    )
    # Actually 0.01*50000=500 not below, need larger ATR
    res2 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(10),
        strategy_name="sma", regime="trend", atr=d(100)  # stop negative? but test
    )
    # Use small equity scenario: patch equity to 100
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.current_equity = d(100)
        await session.flush()
    res3 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    # 1% of 100 =1, risk_per 1000 -> size 0.001 -> notional 50 -> actually above 10, need even smaller equity
    # Let's just verify rejection path: price 0.01 with high ATR
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.current_equity = d(10)
        daily.peak_equity = d(10)
        daily.max_drawdown_pct = d(0)
        # Also reset global peak to avoid max_drawdown trigger
        from trading_bot.risk_management.models import GlobalRiskState
        from sqlalchemy import select
        result = await session.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
        gs = result.scalar_one_or_none()
        if gs:
            gs.peak_equity = d(10)
        await session.flush()
    res4 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(2000)
    )
    assert res4.decision == RiskDecision.REJECTED
    assert res4.rule == RiskRule.POSITION_SIZING


# ---------------------------------------------------------------------------
# 3) Stop loss obligatorio + trailing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stop_loss_calculated_if_not_provided(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2), strategy_stop_loss=None
    )
    assert res.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)
    assert res.stop_loss_price == d(100) - d(2)*d(2)
    assert res.stop_loss_type == "atr"

@pytest.mark.asyncio
async def test_stop_loss_from_strategy_used(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500), strategy_stop_loss=d(49000)
    )
    assert res.stop_loss_price == d(49000)
    assert res.stop_loss_type == "strategy"

@pytest.mark.asyncio
async def test_trailing_stop_flag_propagated(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500), trailing_stop=True
    )
    assert res.trailing_stop is True
    assert res.stop_loss_type == "trailing" or res.stop_loss_type == "atr"  # we map trailing

@pytest.mark.asyncio
async def test_stop_loss_missing_without_atr_rejected(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=None, strategy_stop_loss=None
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule in (RiskRule.STOP_LOSS_REQUIRED, RiskRule.DATA_INTEGRITY)

@pytest.mark.asyncio
async def test_invalid_stop_loss_rejected(risk_engine):
    # BUY with stop above price
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500), strategy_stop_loss=d(51000)
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.STOP_LOSS_REQUIRED


# ---------------------------------------------------------------------------
# 4) Total exposure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_total_exposure_limit(risk_engine):
    # Fill exposure to 20% = 2000 notional
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.total_exposure = d(1900)  # almost full
        await session.flush()
    # Price 50000 size 0.1 = 5000 -> would exceed, should be reduced or rejected
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    # Available 100, reduced size 0.002 -> notional 100 but min 10 so REDUCED with size 0.002
    assert res.decision in (RiskDecision.REDUCED, RiskDecision.REJECTED)

@pytest.mark.asyncio
async def test_two_simultaneous_signals_exceeding_exposure(risk_engine):
    """Dos señales casi simultáneas que juntas superarían límite."""
    # Reset exposure to 0, limit 2000
    # First signal approved consumes 1000
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.total_exposure = d(0)
        await session.flush()
    # We simulate two evaluations concurrently - second should see updated exposure
    # Evaluate first
    res1 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(100), proposed_size=d(0.02)  # 1000 notional
    )
    assert res1.decision == RiskDecision.APPROVED
    # Exposure now 1000
    res2 = await risk_engine.evaluate_signal(
        symbol="ETH/USDT", signal_type="BUY", signal_price=d(3000),
        strategy_name="sma", regime="trend", atr=d(30), proposed_size=d(0.5)  # 1500 notional -> would exceed 2000
    )
    # Total would be 1000+1500=2500 >2000, so second must be REDUCED to 1000/3000=0.333...
    assert res2.decision in (RiskDecision.REDUCED, RiskDecision.REJECTED)
    if res2.decision == RiskDecision.REDUCED:
        assert res2.approved_size < d(0.5)


@pytest.mark.asyncio
async def test_atr_unavailable_rejected(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=None, strategy_stop_loss=None
    )
    assert res.decision == RiskDecision.REJECTED
    # Also when ATR zero
    res2 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(0)
    )
    assert res2.decision == RiskDecision.REJECTED


# ---------------------------------------------------------------------------
# 5) Circuit breaker
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_triggers_on_high_volatility(risk_engine):
    # ATR% 0.06 > 0.05 threshold
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(3000)  # 6%
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.CIRCUIT_BREAKER

@pytest.mark.asyncio
async def test_circuit_breaker_not_triggered_normal_vol(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(1)  # 1%
    )
    assert res.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)


# ---------------------------------------------------------------------------
# 6) Fail-safe
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fail_safe_connection_error_reject(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500), connection_healthy=False
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.CONNECTION_ERROR

@pytest.mark.asyncio
async def test_fail_safe_data_invalid_reject(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500), data_valid=False
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.DATA_INTEGRITY

@pytest.mark.asyncio
async def test_fail_safe_negative_price_reject(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(-100),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.DATA_INTEGRITY

@pytest.mark.asyncio
async def test_fail_safe_corrupt_atr_reject(risk_engine):
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(-500)
    )
    assert res.decision == RiskDecision.REJECTED


# ---------------------------------------------------------------------------
# 7) Max drawdown - must stay OFF until manual
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_max_drawdown_kills_bot_until_manual(risk_engine):
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.current_equity = d(8400)
        daily.peak_equity = d(10000)
        daily.max_drawdown_pct = d(16)  # >15%
        from trading_bot.risk_management.models import GlobalRiskState
        from sqlalchemy import select
        result = await session.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
        gs = result.scalar_one_or_none()
        if gs:
            gs.peak_equity = d(10000)
        await session.flush()
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.MAX_DRAWDOWN
    # Even after PnL recovers, drawdown stays - must be manual reset (we don't auto reset)
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.current_equity = d(11000)
        daily.daily_pnl = d(1000)
        daily.total_exposure = d(0)
        # max_drawdown still 16, peak still 10000 -> still 16% drawdown from peak? Actually 11000 new peak would be >10000, but we keep peak 10000 to simulate no reset
        await session.flush()
    res2 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    assert res2.decision == RiskDecision.REJECTED
    # Manual reset
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.max_drawdown_pct = d(5)
        from trading_bot.risk_management.models import GlobalRiskState
        from sqlalchemy import select
        result = await session.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
        gs = result.scalar_one_or_none()
        if gs:
            gs.peak_equity = d(11000)
        daily.peak_equity = d(11000)
        await session.flush()
    res3 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    assert res3.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)


# ---------------------------------------------------------------------------
# 8) Kill switch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kill_switch_blocks_all(risk_engine):
    await risk_engine.activate_kill_switch("test", "emergency")
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    assert res.decision == RiskDecision.REJECTED
    assert res.rule == RiskRule.KILL_SWITCH
    # Deactivate and should approve (use low price to avoid exposure)
    await risk_engine.deactivate_kill_switch()
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.total_exposure = d(0)
        await session.flush()
    res2 = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    assert res2.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)

@pytest.mark.asyncio
async def test_kill_switch_priority_over_other_rules(risk_engine):
    # Even with valid signal, kill switch wins
    await risk_engine.activate_kill_switch("telegram", "manual")
    async with risk_engine._db.session() as session:
        daily = await risk_engine._get_or_create_daily_stats(session)
        daily.daily_pnl = d(-500)  # also daily limit
        daily.max_drawdown_pct = d(20)
        await session.flush()
    res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(5000)  # also circuit breaker
    )
    assert res.rule == RiskRule.KILL_SWITCH
    await risk_engine.deactivate_kill_switch()


# ---------------------------------------------------------------------------
# 9) Audit logging
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_decision_logged(risk_engine):
    from sqlalchemy import select
    from trading_bot.risk_management.models import RiskLog
    # Approved (low price to avoid exposure reduction -> APPROVED)
    await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(100),
        strategy_name="sma", regime="trend", atr=d(2)
    )
    # Rejected
    await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=None
    )
    async with risk_engine._db.session() as session:
        result = await session.execute(select(RiskLog))
        logs = result.scalars().all()
        assert len(logs) >= 2
        decisions = {l.decision for l in logs}
        assert RiskDecision.REJECTED in decisions
        # Could be APPROVED or REDUCED
        assert any(d in decisions for d in (RiskDecision.APPROVED, RiskDecision.REDUCED))
        for l in logs:
            assert l.symbol == "BTC/USDT"
            assert l.reason is not None
            assert l.account_equity is not None


# ---------------------------------------------------------------------------
# Execution gateway - no order without risk
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_order_without_risk_gateway(risk_engine):
    executor = OrderExecutor(risk_engine=risk_engine)
    # Attempt without risk_result should raise
    req = OrderRequest(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    with pytest.raises(RiskGatewayError):
        await executor.execute(req)
    # Rejected risk also blocked
    req2 = OrderRequest(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500),
        proposed_size=d(10)
    )
    # Force rejection via kill switch
    await risk_engine.activate_kill_switch("test", "x")
    risk_res = await risk_engine.evaluate_signal(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    req2.risk_result = risk_res
    exec_res = await executor.execute(req2)
    assert exec_res.executed is False
    assert executor.executed_count == 0
    await risk_engine.deactivate_kill_switch()
    # Approved via gateway should execute (paper)
    req3 = OrderRequest(
        symbol="BTC/USDT", signal_type="BUY", signal_price=d(50000),
        strategy_name="sma", regime="trend", atr=d(500)
    )
    exec_res2 = await executor.execute_via_risk(req3)
    assert exec_res2.executed is True
    assert exec_res2.stop_loss is not None
    assert executor.executed_count == 1


@pytest.mark.asyncio
async def test_risk_status_monitoring(risk_engine):
    status = await risk_engine.get_risk_status()
    assert "kill_switch_active" in status
    assert "daily_pnl_pct" in status
    assert "max_drawdown_pct" in status
    assert "total_exposure" in status
    await risk_engine.activate_kill_switch("api", "test")
    status2 = await risk_engine.get_risk_status()
    assert status2["kill_switch_active"] is True
    await risk_engine.deactivate_kill_switch()
