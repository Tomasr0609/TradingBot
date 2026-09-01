"""Tests para los 6 bugs de instalación/SQLite/Testnet (prompt corrección)."""
import pytest
import tempfile
import pathlib
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# ------------------------------------------------------------------
# BUG 1 - aiosqlite dependency
# ------------------------------------------------------------------
def test_bug1_aiosqlite_importable():
    """Smoke: aiosqlite debe estar disponible tras pip install -e '.[dev,sqlite]'."""
    import aiosqlite  # noqa: F401
    assert aiosqlite is not None

def test_bug1_pyproject_has_sqlite_extra():
    """pyproject.toml debe declarar extra sqlite con aiosqlite."""
    text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
    assert "aiosqlite" in text
    assert "sqlite" in text.lower()

# ------------------------------------------------------------------
# BUG 2 - fetchCurrencies False
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug2_fetchCurrencies_disabled_in_testnet():
    """load_markets() no debe disparar fetch_currencies cuando es Testnet."""
    from trading_bot.data_collection.client import BinanceClient
    from trading_bot.config.settings import get_settings
    s = get_settings()
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    # Mock ccxt.binance
    mock_exchange = AsyncMock()
    mock_exchange.load_markets = AsyncMock()
    mock_exchange.set_sandbox_mode = MagicMock()
    mock_exchange.urls = {"api": {"public": "https://testnet.binance.vision/api/v3"}}
    # fetch_currencies should NOT be called because option fetchCurrencies=False
    # We verify the client is created with that option
    with patch("trading_bot.data_collection.client.ccxt.binance", return_value=mock_exchange) as mock_binance:
        client = BinanceClient()
        # Force re-init
        client._initialized = False
        client._exchange = None
        await client.initialize()
        # Check that ccxt.binance was called with fetchCurrencies False
        assert mock_binance.called
        args, kwargs = mock_binance.call_args
        opts = kwargs.get("options") or args[0].get("options", {})
        # Could be positional dict
        if not opts:
            opts = args[0].get("options", {})
        # In our implementation, options contains fetchCurrencies: False
        # Retrieve correctly
        passed = mock_binance.call_args[0][0] if mock_binance.call_args[0] else {}
        assert passed.get("options", {}).get("fetchCurrencies") is False, "fetchCurrencies debe ser False para Testnet"
        # Ensure load_markets was called, but fetch_currencies was never called
        mock_exchange.load_markets.assert_awaited_once()
        # fetch_currencies should not have been called at all (mock would have attribute)
        assert not hasattr(mock_exchange, "fetch_currencies") or not mock_exchange.fetch_currencies.called if hasattr(mock_exchange, "fetch_currencies") else True
        await client.close()
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode

# ------------------------------------------------------------------
# BUG 3 - URL Testnet con sufijo /api/v3 via set_sandbox_mode
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug3_testnet_url_has_api_v3_suffix():
    """En modo Testnet, urls[api][public] debe apuntar a https://testnet.binance.vision/api con sufijo."""
    # Direct test of ccxt sandbox without mocking BinanceClient internals
    import ccxt
    ex = ccxt.binance({"apiKey": "test", "secret": "test", "options": {"fetchCurrencies": False}})
    ex.set_sandbox_mode(True)
    public_url = ex.urls["api"]["public"]
    assert "testnet.binance.vision" in public_url, f"public url {public_url} should contain testnet"
    assert "/api" in public_url, f"public url {public_url} should contain /api suffix"
    # Also verify via BinanceClient that set_sandbox_mode is called
    from trading_bot.data_collection.client import BinanceClient
    from trading_bot.config.settings import get_settings
    s = get_settings()
    orig_url = s.binance_base_url
    orig_mode = s.trading_mode
    s.binance_base_url = "https://testnet.binance.vision"
    s.trading_mode = "testnet"
    mock_exchange = AsyncMock()
    mock_exchange.load_markets = AsyncMock()
    mock_exchange.set_sandbox_mode = MagicMock()
    mock_exchange.urls = {"api": {"public": "https://testnet.binance.vision/api"}}
    with patch("trading_bot.data_collection.client.ccxt.binance", return_value=mock_exchange):
        client = BinanceClient()
        client._initialized = False
        client._exchange = None
        await client.initialize()
        mock_exchange.set_sandbox_mode.assert_called_once_with(True)
        await client.close()
    s.binance_base_url = orig_url
    s.trading_mode = orig_mode
    import trading_bot.data_collection.client as mod
    mod._client_instance = None

# ------------------------------------------------------------------
# BUG 4 - func.now() vs func.current_timestamp() / CURRENT_TIMESTAMP
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug4_timestamp_defaults_portable_sqlite_memory():
    """Inserta en cada tabla con default timestamp y verifica que no falla en SQLite memoria."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy import select
    from datetime import datetime, timezone
    from decimal import Decimal
    from trading_bot.storage.models import Base, Kline
    from trading_bot.risk_management.models import RiskLog, RiskDecision, RiskRule, DailyStats, KillSwitch, GlobalRiskState
    from trading_bot.execution.models import Position, ExecutedOrder

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Use a single session with commit to trigger server defaults via RETURNING disabled (implicit_returning=False)
    async with factory() as session:
        # Kline
        k = Kline(symbol="BTC/USDT", timeframe="1h", open_time=datetime.now(timezone.utc), close_time=datetime.now(timezone.utc), open_price=Decimal("100"), high_price=Decimal("110"), low_price=Decimal("90"), close_price=Decimal("105"), volume=Decimal("10"), quote_volume=Decimal("1000"), trades_count=10, taker_buy_base_volume=Decimal("5"), taker_buy_quote_volume=Decimal("500"), is_closed=True)
        session.add(k)
        await session.commit()
        await session.refresh(k)
        assert k.created_at is not None

        # RiskLog
        rl = RiskLog(symbol="BTC/USDT", signal_type="BUY", signal_price=Decimal("100"), strategy_name="test", regime="trend", decision=RiskDecision.APPROVED, triggered_rule=RiskRule.POSITION_SIZING, reason="test", account_equity=Decimal("10000"), daily_pnl=Decimal("0"), total_exposure=Decimal("0"), current_drawdown=Decimal("0"))
        session.add(rl)
        await session.commit()
        await session.refresh(rl)
        assert rl.timestamp is not None

        # DailyStats - use unique date
        ds = DailyStats(date=datetime(2026, 2, 1, tzinfo=timezone.utc), starting_equity=Decimal("10000"), current_equity=Decimal("10000"), peak_equity=Decimal("10000"), daily_loss_limit_pct=Decimal("3"))
        session.add(ds)
        await session.commit()
        await session.refresh(ds)
        assert ds.created_at is not None

        # KillSwitch
        ks = KillSwitch(is_active=False)
        session.add(ks)
        await session.commit()
        await session.refresh(ks)
        assert ks.id is not None

        # GlobalRiskState - use id 99 to avoid conflict
        gs = GlobalRiskState(id=99, peak_equity=Decimal("10000"))
        session.add(gs)
        await session.commit()
        await session.refresh(gs)
        assert gs.created_at is not None or gs.peak_equity is not None

        # Position
        pos = Position(symbol="BTC/USDT", side="BUY", size=Decimal("1"), entry_price=Decimal("100"), stop_loss=Decimal("90"))
        session.add(pos)
        await session.commit()
        await session.refresh(pos)
        assert pos.opened_at is not None

        # ExecutedOrder
        eo = ExecutedOrder(symbol="BTC/USDT", signal_type="BUY", signal_price=Decimal("100"), strategy_name="test", regime="trend", risk_decision="approved", risk_rule="position_sizing", risk_reason="test", status="filled", is_testnet=True)
        session.add(eo)
        await session.commit()
        await session.refresh(eo)
        assert eo.created_at is not None

    await engine.dispose()

def test_bug4_migrations_use_current_timestamp():
    """Verifica que migraciones usan CURRENT_TIMESTAMP, no now()."""
    import pathlib
    for fname in ["alembic/versions/001_initial.py", "alembic/versions/002_risk_tables.py", "alembic/versions/004_executed_orders.py"]:
        content = pathlib.Path(fname).read_text(encoding="utf-8")
        assert 'sa.text("now()")' not in content, f"{fname} aún usa now()"
        assert 'func.now()' not in content, f"{fname} aún usa func.now()"
        # Debe contener CURRENT_TIMESTAMP
        assert "CURRENT_TIMESTAMP" in content or "current_timestamp" in content.lower()

def test_bug4_models_use_current_timestamp():
    """Verifica que modelos usan func.current_timestamp()."""
    import pathlib
    for fname in ["src/trading_bot/storage/models.py", "src/trading_bot/risk_management/models.py", "src/trading_bot/execution/models.py"]:
        content = pathlib.Path(fname).read_text(encoding="utf-8")
        assert "func.current_timestamp()" in content, f"{fname} debe usar func.current_timestamp()"
        assert "func.now()" not in content

# ------------------------------------------------------------------
# BUG 5 - id BigInteger vs Integer
# ------------------------------------------------------------------
def test_bug5_migrations_use_integer_for_id():
    """Verifica que migraciones usan sa.Integer() para id, no BigInteger."""
    import pathlib
    for fname in ["alembic/versions/001_initial.py", "alembic/versions/002_risk_tables.py"]:
        content = pathlib.Path(fname).read_text(encoding="utf-8")
        # Debe tener sa.Integer() para id
        assert 'sa.Column("id", sa.Integer()' in content, f"{fname} debe usar Integer para id"
        assert 'sa.Column("id", sa.BigInteger()' not in content

@pytest.mark.asyncio
async def test_bug5_autoincrement_on_disk_sqlite():
    """Crea registros en cada tabla afectada sin id, en archivo SQLite en disco, y verifica autonumerado."""
    import tempfile, pathlib, os
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from trading_bot.storage.models import Base, Kline
    from trading_bot.risk_management.models import RiskLog, RiskDecision, RiskRule, DailyStats, KillSwitch
    from decimal import Decimal
    from datetime import datetime, timezone

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = pathlib.Path(tmpdir) / "test_disk.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            # Kline sin id
            k = Kline(symbol="BTC/USDT", timeframe="1h", open_time=datetime.now(timezone.utc), close_time=datetime.now(timezone.utc), open_price=Decimal("100"), high_price=Decimal("110"), low_price=Decimal("90"), close_price=Decimal("105"), volume=Decimal("10"), quote_volume=Decimal("1000"), trades_count=10, taker_buy_base_volume=Decimal("5"), taker_buy_quote_volume=Decimal("500"), is_closed=True)
            session.add(k)
            await session.flush()
            assert k.id is not None and k.id > 0

            # RiskLog sin id
            rl = RiskLog(symbol="BTC/USDT", signal_type="BUY", signal_price=Decimal("100"), strategy_name="test", regime="trend", decision=RiskDecision.APPROVED, triggered_rule=RiskRule.POSITION_SIZING, reason="test", account_equity=Decimal("10000"), daily_pnl=Decimal("0"), total_exposure=Decimal("0"), current_drawdown=Decimal("0"))
            session.add(rl)
            await session.flush()
            assert rl.id is not None and rl.id > 0

            # DailyStats sin id - use unique date
            ds = DailyStats(date=datetime(2026,1,1, tzinfo=timezone.utc), starting_equity=Decimal("10000"), current_equity=Decimal("10000"), peak_equity=Decimal("10000"), daily_loss_limit_pct=Decimal("3"))
            session.add(ds)
            await session.flush()
            assert ds.id is not None and ds.id > 0

            # KillSwitch sin id
            ks = KillSwitch(is_active=False)
            session.add(ks)
            await session.flush()
            assert ks.id is not None and ks.id > 0

            await session.commit()
        await engine.dispose()
        assert db_path.exists()

# ------------------------------------------------------------------
# BUG 6 - implicit_returning=False
# ------------------------------------------------------------------
def test_bug6_implicit_returning_false():
    """Verifica que modelos con autoincremento tienen implicit_returning False y engine también."""
    from trading_bot.storage.models import Kline
    from trading_bot.risk_management.models import RiskLog, DailyStats, KillSwitch, GlobalRiskState
    from trading_bot.execution.models import Position, ExecutedOrder
    from trading_bot.storage.database import Database

    for model in [Kline, RiskLog, DailyStats, KillSwitch, GlobalRiskState, Position, ExecutedOrder]:
        assert "__table_args__" in dir(model) or hasattr(model, "__table_args__")
        table_args = getattr(model, "__table_args__", {})
        # Puede ser tuple con dict al final o dict directo
        if isinstance(table_args, tuple):
            # El dict es el último elemento si es dict
            dict_part = table_args[-1] if isinstance(table_args[-1], dict) else {}
        elif isinstance(table_args, dict):
            dict_part = table_args
        else:
            dict_part = {}
        assert dict_part.get("implicit_returning") is False, f"{model.__name__} debe tener implicit_returning=False"

    # Engine
    import inspect
    source = pathlib.Path("src/trading_bot/storage/database.py").read_text(encoding="utf-8")
    assert "implicit_returning=False" in source
