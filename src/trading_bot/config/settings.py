"""Configuration settings for the trading bot."""

from functools import lru_cache
from typing import List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Binance Testnet
    binance_api_key: str = Field(..., description="Binance Testnet API Key")
    binance_api_secret: str = Field(..., description="Binance Testnet API Secret")
    binance_base_url: str = Field(
        default="https://testnet.binance.vision",
        description="Binance base URL (must be testnet)",
    )

    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./trading_bot.db",
        description="Database connection URL",
    )

    # Telegram
    telegram_bot_token: str = Field(default="", description="Telegram Bot Token")
    telegram_chat_id: str = Field(default="", description="Telegram Chat ID for notifications")

    # Risk Management (conservative defaults)
    risk_max_daily_loss_pct: float = Field(
        default=0.03, description="Max daily loss as % of capital"
    )
    risk_max_position_risk_pct: float = Field(
        default=0.01, description="Max risk per position as % of capital"
    )
    risk_max_total_exposure_pct: float = Field(
        default=0.20, description="Max total exposure as % of capital"
    )
    risk_max_drawdown_pct: float = Field(
        default=0.15, description="Max drawdown from peak before kill switch"
    )
    risk_volatility_threshold_pct: float = Field(
        default=0.05, description="Volatility threshold for circuit breaker"
    )

    # Trading Configuration
    trading_symbols: List[str] = Field(
        default=["BTC/USDT", "ETH/USDT"], description="Symbols to trade"
    )
    trading_timeframe: str = Field(default="1h", description="Primary timeframe")
    trading_mode: str = Field(default="testnet", description="Trading mode: testnet|paper|live")

    # Sentiment / News Filter (Fase 5 - opcional, solo reduce/veta)
    sentiment_enabled: bool = Field(default=False, description="Enable sentiment filter (Phase 5)")
    cryptopanic_token: str = Field(default="", description="CryptoPanic API token (optional, public works)")
    news_api_key: str = Field(default="", description="NewsAPI key (optional)")
    sentiment_veto_threshold: float = Field(default=-0.6, description="Tone score for veto")
    sentiment_reduce_threshold: float = Field(default=-0.3, description="Tone score for reduce")
    sentiment_relevance_veto: float = Field(default=0.8, description="Relevance for veto")
    sentiment_relevance_reduce: float = Field(default=0.6, description="Relevance for reduce")
    sentiment_reduce_factor: float = Field(default=0.5, description="Reduce factor 0..1 (0.5 = 50% size)")

    # Macro events pause (FOMC etc) - JSON array in env MACRO_EVENTS_JSON
    macro_events_json: str = Field(default="", description='JSON list [{"name":"FOMC","time":"2026-09-17T18:00:00Z","before":2,"after":2}]')

    # General
    log_level: str = Field(default="INFO", description="Logging level")
    timezone: str = Field(default="UTC", description="Timezone for reports")

    @property
    def is_testnet(self) -> bool:
        """Verify we're using testnet."""
        return "testnet" in self.binance_base_url.lower()

    @property
    def symbols_list(self) -> List[str]:
        """Parse symbols from comma-separated string if needed."""
        if isinstance(self.trading_symbols, str):
            return [s.strip() for s in self.trading_symbols.split(",")]
        return self.trading_symbols


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()