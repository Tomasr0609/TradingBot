"""TradingBot - Loop principal orquestador (Fase 4).

Recolección -> Análisis -> Decisión -> Riesgo -> Ejecución -> Registro
Resiliente a errores parciales: un módulo que falle no tumba todo el proceso.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
import pandas as pd
from sqlalchemy import select

from trading_bot.config.settings import get_settings
from trading_bot.storage.database import get_database, init_db
from trading_bot.storage.models import Kline
from trading_bot.analysis.indicators import compute_indicators, get_market_regime
from trading_bot.decision.strategies import get_strategy
from trading_bot.risk_management.engine import RiskEngine
from trading_bot.risk_management.models import RiskDecision
from trading_bot.execution.executor import OrderExecutor, OrderRequest, TestnetEnforcementError
from trading_bot.data_collection.client import get_binance_client
from trading_bot.notifications.telegram import get_telegram_notifier

logger = logging.getLogger(__name__)


def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1]
    val = int(tf[:-1])
    if unit == "m":
        return val * 60
    if unit == "h":
        return val * 3600
    if unit == "d":
        return val * 86400
    return 3600


class TradingBot:
    def __init__(self):
        self.settings = get_settings()
        self.db = get_database()
        self.risk_engine = RiskEngine()
        self.exchange_client = None
        self.executor = None
        self.telegram = None
        self.running = False
        # Validate testnet strictly at startup
        self._assert_testnet()

    def _assert_testnet(self):
        # Fase 6: si es live, pasa por validación estricta de producción
        if self.settings.trading_mode == "live":
            from trading_bot.config.production import assert_live_trading_authorized
            assert_live_trading_authorized(self.settings)
            return
        if not self.settings.is_testnet:
            raise TestnetEnforcementError(
                f"BINANCE_BASE_URL no es testnet: {self.settings.binance_base_url}. "
                "Fase 4 solo Testnet. Si no estás seguro de la key, PARA y pregunta."
            )
        if self.settings.trading_mode not in ("testnet", "paper"):
            raise TestnetEnforcementError(f"TRADING_MODE={self.settings.trading_mode} no permitido en Fase 4 (solo testnet/paper)")

    async def initialize(self):
        await init_db()
        # Init exchange client
        if self.settings.trading_mode in ("testnet", "live"):
            self.exchange_client = get_binance_client()
            try:
                await self.exchange_client.initialize()
                logger.info(f"Exchange client ready {self.settings.binance_base_url} mode={self.settings.trading_mode}")
                if self.settings.trading_mode == "live":
                    from trading_bot.config.production import verify_api_key_restrictions
                    await verify_api_key_restrictions(self.exchange_client._exchange if hasattr(self.exchange_client, "_exchange") and self.exchange_client._exchange else self.exchange_client)
            except Exception as e:
                # En live, cualquier fallo es fatal
                if self.settings.trading_mode == "live":
                    raise
                logger.warning(f"Exchange init failed (continuará en paper si falla): {e}")
                self.exchange_client = None
        self.executor = OrderExecutor(risk_engine=self.risk_engine, exchange_client=self.exchange_client._exchange if self.exchange_client and hasattr(self.exchange_client, "_exchange") else self.exchange_client)
        # Telegram optional
        if self.settings.telegram_bot_token:
            try:
                self.telegram = get_telegram_notifier(self.risk_engine)
                self.telegram.configure(self.settings.telegram_bot_token, self.settings.telegram_chat_id)
                await self.telegram.start()
            except Exception as e:
                logger.warning(f"Telegram failed to start: {e}")
        logger.info("TradingBot initialized")

    async def shutdown(self):
        self.running = False
        if self.telegram:
            try:
                await self.telegram.stop()
            except Exception:
                pass
        if self.exchange_client:
            try:
                await self.exchange_client.close()
            except Exception:
                pass
        logger.info("TradingBot shutdown")

    async def _load_recent_candles(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        async with self.db.session() as session:
            stmt = select(Kline).where(Kline.symbol == symbol, Kline.timeframe == timeframe, Kline.is_closed == True).order_by(Kline.open_time.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            if len(rows) < 50:
                # Try to fetch via REST if not enough data
                logger.warning(f"Insufficient candles for {symbol} {timeframe}: {len(rows)}")
                # Fallback: try historical ingestion
                try:
                    from trading_bot.data_collection.historical import HistoricalDataIngester
                    ingester = HistoricalDataIngester(session)
                    await ingester.ingest_symbol(symbol, timeframe, days_back=7)
                    await session.flush()
                    # retry
                    result = await session.execute(stmt)
                    rows = result.scalars().all()
                except Exception as e:
                    logger.error(f"Historical fetch failed for {symbol}: {e}")
            if not rows:
                return pd.DataFrame()
            data = []
            for k in reversed(rows):
                data.append({"open_time": k.open_time, "open": float(k.open_price), "high": float(k.high_price), "low": float(k.low_price), "close": float(k.close_price), "volume": float(k.volume)})
            df = pd.DataFrame(data)
            df.set_index("open_time", inplace=True)
            df.index = pd.to_datetime(df.index, utc=True)
            return df

    async def run_once(self):
        """Una iteración del loop para todos los símbolos. Resiliente: errores por símbolo no tumban todo."""
        symbols = self.settings.symbols_list if isinstance(self.settings.trading_symbols, list) else [s.strip() for s in str(self.settings.trading_symbols).split(",")]
        timeframe = self.settings.trading_timeframe
        # Strategy from config or default composite
        try:
            strategy = get_strategy("composite")
        except Exception:
            from trading_bot.decision.strategies import CompositeStrategy
            strategy = CompositeStrategy()

        for symbol in symbols:
            try:
                await self._process_symbol(symbol, timeframe, strategy)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)
                # Notificar pero no tumbar loop
                if self.telegram:
                    try:
                        await self.telegram.notify_risk_rejection(symbol, str(e), "bot_error")
                    except Exception:
                        pass
                continue

    async def _process_symbol(self, symbol: str, timeframe: str, strategy):
        # 1) Recolección
        try:
            df = await self._load_recent_candles(symbol, timeframe)
        except Exception as e:
            logger.error(f"Data collection failed {symbol}: {e}")
            # Fail safe: notify and skip
            return
        if df.empty or len(df) < 50:
            logger.warning(f"Skip {symbol}: not enough data {len(df)}")
            return

        # 2) Análisis: indicators already inside strategy, but we need atr for risk
        try:
            df_ind = compute_indicators(df.copy())
            regime = get_market_regime(df_ind)
            atr = None
            if "atr_14" in df_ind.columns and not pd.isna(df_ind["atr_14"].iloc[-1]):
                atr = Decimal(str(df_ind["atr_14"].iloc[-1]))
            last_close = Decimal(str(df_ind["close"].iloc[-1]))
        except Exception as e:
            logger.error(f"Analysis failed {symbol}: {e}")
            return

        # 3) Decisión: signals
        try:
            signals = strategy.generate_signals(df)
            # Only act on last closed candle signal
            last_signal = int(signals.iloc[-1]) if len(signals) else 0
            if last_signal == 0:
                logger.debug(f"No signal {symbol} last={last_signal} regime={regime}")
                return
            signal_type = "BUY" if last_signal == 1 else "SELL"
            # Stop loss from strategy
            idx = len(df_ind) - 1
            from trading_bot.decision.strategies import SignalType
            st = SignalType.BUY if last_signal == 1 else SignalType.SELL
            sl = strategy.get_stop_loss(df_ind, idx, st)
            sl_dec = Decimal(str(sl)) if sl is not None else None
        except Exception as e:
            logger.error(f"Decision failed {symbol}: {e}")
            return

        # 4) Riesgo -> 5) Ejecución (via gateway) - Fase 5: sentimiento como filtro
        sentiment_result = None
        macro_events = None
        if self.settings.sentiment_enabled:
            try:
                from trading_bot.sentiment.provider import CryptoPanicProvider
                from trading_bot.sentiment.filter import SentimentFilter, parse_macro_events_from_env
                provider = CryptoPanicProvider(auth_token=self.settings.cryptopanic_token or None)
                headlines = await provider.fetch_recent(symbol, hours=24, limit=20)
                filt = SentimentFilter(
                    veto_threshold=self.settings.sentiment_veto_threshold,
                    reduce_threshold=self.settings.sentiment_reduce_threshold,
                    relevance_veto=self.settings.sentiment_relevance_veto,
                    relevance_reduce=self.settings.sentiment_relevance_reduce,
                    reduce_factor=self.settings.sentiment_reduce_factor,
                )
                sentiment_result = filt.evaluate(headlines, symbol)
                if sentiment_result.action != "allow":
                    logger.info(f"Sentiment {symbol}: {sentiment_result.action} {sentiment_result.reason}")
                macro_events = parse_macro_events_from_env(self.settings.macro_events_json)
            except Exception as e:
                logger.warning(f"Sentiment fetch failed {symbol}: {e} - fail safe allow")
                sentiment_result = None
        else:
            # Even if disabled, still check macro pause if configured
            try:
                from trading_bot.sentiment.filter import parse_macro_events_from_env
                if self.settings.macro_events_json:
                    from trading_bot.sentiment.filter import parse_macro_events_from_env as pm
                    macro_events = pm(self.settings.macro_events_json)
            except Exception:
                pass

        try:
            req = OrderRequest(
                symbol=symbol,
                signal_type=signal_type,
                signal_price=last_close,
                strategy_name=strategy.name,
                regime=regime,
                atr=atr,
                strategy_stop_loss=sl_dec,
                proposed_size=None,
                trailing_stop=False,
                sentiment_result=sentiment_result,
                macro_events=macro_events,
            )
            result = await self.executor.execute_via_risk(req)
            # 5) Notificaciones
            if result.executed:
                logger.info(f"Executed {symbol} {signal_type} size={result.executed_size} price={result.executed_price} SL={result.stop_loss} id={result.order_id}")
                if self.telegram:
                    await self.telegram.notify_trade_executed(symbol, signal_type, float(result.executed_size), float(result.executed_price), float(result.stop_loss) if result.stop_loss else None)
            else:
                logger.info(f"Blocked {symbol} {signal_type}: {result.reason}")
                # Notify only for interesting rejections
                if any(k in result.reason for k in ["Daily loss", "Circuit breaker", "Max drawdown"]):
                    if self.telegram:
                        if "Daily loss" in result.reason:
                            status = await self.risk_engine.get_risk_status()
                            await self.telegram.notify_daily_limit_reached(status["daily_pnl_pct"])
                        elif "Circuit breaker" in result.reason:
                            await self.telegram.notify_circuit_breaker(symbol, float(atr/last_close*100) if atr else 0, float(self.settings.risk_volatility_threshold_pct*100))
                        elif "Max drawdown" in result.reason:
                            status = await self.risk_engine.get_risk_status()
                            await self.telegram.notify_max_drawdown(status["max_drawdown_pct"], float(self.settings.risk_max_drawdown_pct*100))
                        else:
                            await self.telegram.notify_risk_rejection(symbol, result.reason, "risk")
        except Exception as e:
            logger.error(f"Risk/Execution failed {symbol}: {e}", exc_info=True)
            return

    async def run_forever(self):
        self.running = True
        interval = timeframe_to_seconds(self.settings.trading_timeframe)
        # For 1h timeframe, run every 60s checking for new closed candle; for 1m, every 30s
        poll = min(60, interval // 2) if interval > 60 else 30
        logger.info(f"Starting main loop interval {interval}s poll {poll}s symbols={self.settings.symbols_list}")
        while self.running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Loop iteration failed: {e}", exc_info=True)
            try:
                await asyncio.sleep(poll)
            except asyncio.CancelledError:
                break
