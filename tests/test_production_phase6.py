"""Tests Fase 6 - Transición a real solo con autorización explícita."""

import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from trading_bot.config.settings import get_settings
from trading_bot.config.production import assert_live_trading_authorized, LiveTradingNotAuthorized, EXPECTED_PHRASE

def clear_live_env():
    for k in ["ALLOW_LIVE_TRADING","CONFIRM_LIVE_TRADING_I_UNDERSTAND_RISK","LIVE_TRADING_CONFIRM_PHRASE","LIVE_ALLOWED_IPS"]:
        os.environ.pop(k, None)

def set_live_env(authorized=True):
    clear_live_env()
    if authorized:
        os.environ["ALLOW_LIVE_TRADING"]="true"
        os.environ["CONFIRM_LIVE_TRADING_I_UNDERSTAND_RISK"]="true"
        os.environ["LIVE_TRADING_CONFIRM_PHRASE"]=EXPECTED_PHRASE

def test_blocks_without_any_flag():
    s=get_settings()
    orig_url=s.binance_base_url; orig_mode=s.trading_mode
    orig_daily=s.risk_max_daily_loss_pct; orig_pos=s.risk_max_position_risk_pct; orig_exp=s.risk_max_total_exposure_pct
    try:
        s.binance_base_url="https://api.binance.com"
        s.trading_mode="live"
        s.risk_max_daily_loss_pct=0.01; s.risk_max_position_risk_pct=0.005; s.risk_max_total_exposure_pct=0.10
        clear_live_env()
        with pytest.raises(LiveTradingNotAuthorized, match="ALLOW_LIVE_TRADING"):
            assert_live_trading_authorized(s)
    finally:
        s.binance_base_url=orig_url; s.trading_mode=orig_mode
        s.risk_max_daily_loss_pct=orig_daily; s.risk_max_position_risk_pct=orig_pos; s.risk_max_total_exposure_pct=orig_exp
        clear_live_env()

def test_blocks_without_second_flag():
    s=get_settings()
    orig_url=s.binance_base_url; orig_mode=s.trading_mode
    orig_daily=s.risk_max_daily_loss_pct; orig_pos=s.risk_max_position_risk_pct; orig_exp=s.risk_max_total_exposure_pct
    try:
        s.binance_base_url="https://api.binance.com"
        s.trading_mode="live"
        s.risk_max_daily_loss_pct=0.01; s.risk_max_position_risk_pct=0.005; s.risk_max_total_exposure_pct=0.10
        clear_live_env()
        os.environ["ALLOW_LIVE_TRADING"]="true"
        with pytest.raises(LiveTradingNotAuthorized, match="CONFIRM_LIVE_TRADING"):
            assert_live_trading_authorized(s)
    finally:
        s.binance_base_url=orig_url; s.trading_mode=orig_mode
        s.risk_max_daily_loss_pct=orig_daily; s.risk_max_position_risk_pct=orig_pos; s.risk_max_total_exposure_pct=orig_exp
        clear_live_env()

def test_blocks_without_phrase():
    s=get_settings()
    orig_url=s.binance_base_url; orig_mode=s.trading_mode
    orig_daily=s.risk_max_daily_loss_pct; orig_pos=s.risk_max_position_risk_pct; orig_exp=s.risk_max_total_exposure_pct
    try:
        s.binance_base_url="https://api.binance.com"
        s.trading_mode="live"
        s.risk_max_daily_loss_pct=0.01; s.risk_max_position_risk_pct=0.005; s.risk_max_total_exposure_pct=0.10
        clear_live_env()
        os.environ["ALLOW_LIVE_TRADING"]="true"
        os.environ["CONFIRM_LIVE_TRADING_I_UNDERSTAND_RISK"]="true"
        os.environ["LIVE_TRADING_CONFIRM_PHRASE"]="wrong phrase"
        with pytest.raises(LiveTradingNotAuthorized, match="LIVE_TRADING_CONFIRM_PHRASE"):
            assert_live_trading_authorized(s)
    finally:
        s.binance_base_url=orig_url; s.trading_mode=orig_mode
        s.risk_max_daily_loss_pct=orig_daily; s.risk_max_position_risk_pct=orig_pos; s.risk_max_total_exposure_pct=orig_exp
        clear_live_env()

def test_blocks_if_limits_not_conservative():
    s=get_settings()
    orig_url=s.binance_base_url; orig_mode=s.trading_mode
    orig_daily=s.risk_max_daily_loss_pct; orig_pos=s.risk_max_position_risk_pct; orig_exp=s.risk_max_total_exposure_pct
    try:
        s.binance_base_url="https://api.binance.com"
        s.trading_mode="live"
        s.risk_max_daily_loss_pct=0.03  # too high (testnet level)
        s.risk_max_position_risk_pct=0.005; s.risk_max_total_exposure_pct=0.10
        set_live_env(True)
        with pytest.raises(LiveTradingNotAuthorized, match="RISK_MAX_DAILY_LOSS"):
            assert_live_trading_authorized(s)
        s.risk_max_daily_loss_pct=0.01
        s.risk_max_position_risk_pct=0.01  # too high
        with pytest.raises(LiveTradingNotAuthorized, match="RISK_MAX_POSITION"):
            assert_live_trading_authorized(s)
    finally:
        s.binance_base_url=orig_url; s.trading_mode=orig_mode
        s.risk_max_daily_loss_pct=orig_daily; s.risk_max_position_risk_pct=orig_pos; s.risk_max_total_exposure_pct=orig_exp
        clear_live_env()

def test_allows_with_correct_authorization():
    s=get_settings()
    orig_url=s.binance_base_url; orig_mode=s.trading_mode
    orig_daily=s.risk_max_daily_loss_pct; orig_pos=s.risk_max_position_risk_pct; orig_exp=s.risk_max_total_exposure_pct
    try:
        s.binance_base_url="https://api.binance.com"
        s.trading_mode="live"
        s.risk_max_daily_loss_pct=0.01; s.risk_max_position_risk_pct=0.005; s.risk_max_total_exposure_pct=0.10
        set_live_env(True)
        # Should not raise
        assert_live_trading_authorized(s)
    finally:
        s.binance_base_url=orig_url; s.trading_mode=orig_mode
        s.risk_max_daily_loss_pct=orig_daily; s.risk_max_position_risk_pct=orig_pos; s.risk_max_total_exposure_pct=orig_exp
        clear_live_env()

def test_blocks_testnet_url_in_live_mode():
    s=get_settings()
    orig_url=s.binance_base_url; orig_mode=s.trading_mode
    try:
        s.binance_base_url="https://testnet.binance.vision"
        s.trading_mode="live"
        set_live_env(True)
        s.risk_max_daily_loss_pct=0.01; s.risk_max_position_risk_pct=0.005; s.risk_max_total_exposure_pct=0.10
        with pytest.raises(LiveTradingNotAuthorized, match="testnet"):
            assert_live_trading_authorized(s)
    finally:
        s.binance_base_url=orig_url; s.trading_mode=orig_mode
        clear_live_env()

@pytest.mark.asyncio
async def test_verify_withdrawals_enabled_blocks():
    from trading_bot.config.production import verify_api_key_restrictions
    # Use simple object with only the method we want
    class FakeExchange:
        async def sapiGetApiRestrictions(self):
            return {"canWithdraw": True, "canTrade": True}
    mock_exchange = FakeExchange()
    with pytest.raises(LiveTradingNotAuthorized, match="retiros"):
        await verify_api_key_restrictions(mock_exchange)

@pytest.mark.asyncio
async def test_verify_withdrawals_disabled_passes():
    from trading_bot.config.production import verify_api_key_restrictions
    class FakeExchange:
        async def sapiGetApiRestrictions(self):
            return {"canWithdraw": False}
    mock_exchange = FakeExchange()
    # Should not raise
    await verify_api_key_restrictions(mock_exchange)

def test_bot_refuses_mainnet_without_phrase():
    from trading_bot.bot import TradingBot
    s=get_settings()
    orig_url=s.binance_base_url; orig_mode=s.trading_mode
    orig_daily=s.risk_max_daily_loss_pct; orig_pos=s.risk_max_position_risk_pct; orig_exp=s.risk_max_total_exposure_pct
    try:
        s.binance_base_url="https://api.binance.com"
        s.trading_mode="live"
        s.risk_max_daily_loss_pct=0.01; s.risk_max_position_risk_pct=0.005; s.risk_max_total_exposure_pct=0.10
        clear_live_env()
        os.environ["ALLOW_LIVE_TRADING"]="true"
        # missing other flags
        with pytest.raises(LiveTradingNotAuthorized):
            TradingBot()
    finally:
        s.binance_base_url=orig_url; s.trading_mode=orig_mode
        s.risk_max_daily_loss_pct=orig_daily; s.risk_max_position_risk_pct=orig_pos; s.risk_max_total_exposure_pct=orig_exp
        clear_live_env()

def test_separate_production_env_file_exists():
    import pathlib
    assert pathlib.Path(".env.production.example").exists()
    content = pathlib.Path(".env.production.example").read_text(encoding="utf-8")
    assert "ALLOW_LIVE_TRADING" in content
    assert "CONFIRM_LIVE_TRADING" in content
    assert "your_MAINNET" in content
    assert "RISK_MAX_DAILY_LOSS_PCT=0.015" in content
