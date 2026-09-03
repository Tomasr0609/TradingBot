"""Risk Management Engine - Core risk rules and position sizing."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from trading_bot.risk_management.models import RiskDecision, RiskRule, RiskLog, DailyStats, KillSwitch
from trading_bot.storage.database import get_database
from trading_bot.config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class RiskResult:
    """Result of risk evaluation."""
    decision: RiskDecision
    rule: RiskRule
    reason: str
    approved_size: Decimal = Decimal("0")
    stop_loss_price: Optional[Decimal] = None
    stop_loss_type: Optional[str] = None
    risk_pct: Optional[Decimal] = None
    account_equity: Optional[Decimal] = None
    daily_pnl: Optional[Decimal] = None
    total_exposure: Optional[Decimal] = None
    current_drawdown: Optional[Decimal] = None
    original_size: Optional[Decimal] = None
    # For trailing stop option
    trailing_stop: bool = False


class RiskEngine:
    """
    Central risk management engine.
    Every signal must pass through this before execution.
    Fails toward the safe side: any uncertainty -> REJECT.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._db = get_database()
        self._cached_equity: Optional[Decimal] = None
        self._cached_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def evaluate_signal(
        self,
        symbol: str,
        signal_type: str,  # "BUY" or "SELL"
        signal_price: Decimal,
        strategy_name: str,
        regime: str,
        atr: Optional[Decimal] = None,
        strategy_stop_loss: Optional[Decimal] = None,
        strategy_take_profit: Optional[Decimal] = None,
        proposed_size: Optional[Decimal] = None,
        trailing_stop: bool = False,
        connection_healthy: bool = True,
        data_valid: bool = True,
        sentiment_result=None,  # SentimentFilterResult | None - Fase 5
        macro_events=None,  # list[MacroEvent] | None
    ) -> RiskResult:
        """
        Evaluate a trading signal against all risk rules.
        Order of checks (priority):
          0) fail-safe: connection/data integrity
          0b) macro pause (FOMC)
          1) kill switch
          2) daily loss limit
          3) max drawdown
          4) circuit breaker (volatility)
          5) position sizing + mandatory stop loss
          5b) sentiment filter (solo reduce/veta, nunca genera)
          6) total exposure
        Every exit path logs to DB.
        """
        # 0) Fail-safe: if caller reports unhealthy connection or invalid data -> REJECT
        if not connection_healthy:
            return await self._reject_and_log(
                symbol, signal_type, signal_price, strategy_name, regime,
                RiskRule.CONNECTION_ERROR, "Connection unhealthy - fail safe reject",
                proposed_size, None, None,
            )
        if not data_valid:
            return await self._reject_and_log(
                symbol, signal_type, signal_price, strategy_name, regime,
                RiskRule.DATA_INTEGRITY, "Data integrity check failed - fail safe reject",
                proposed_size, None, None,
            )
        # Validate price sanity
        if signal_price is None or signal_price <= 0:
            return await self._reject_and_log(
                symbol, signal_type, signal_price or Decimal("0"), strategy_name, regime,
                RiskRule.DATA_INTEGRITY, f"Invalid signal price: {signal_price}",
                proposed_size, None, None,
            )
        if signal_type not in ("BUY", "SELL"):
            return await self._reject_and_log(
                symbol, signal_type, signal_price, strategy_name, regime,
                RiskRule.DATA_INTEGRITY, f"Invalid signal_type: {signal_type}",
                proposed_size, None, None,
            )

        async with self._db.session() as session:
            # 0b) Macro pause (FOMC) - before any trading
            if macro_events:
                from trading_bot.sentiment.filter import is_macro_pause_active
                paused, ev = is_macro_pause_active(macro_events)
                if paused:
                    macro_res = RiskResult(
                        decision=RiskDecision.REJECTED,
                        rule=RiskRule.MACRO_PAUSE,
                        reason=f"Macro pause active: {ev.name} at {ev.event_time} (pause {ev.pause_before_hours}h before / {ev.pause_after_hours}h after)",
                        approved_size=Decimal("0"),
                    )
                    await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, macro_res, proposed_size)
                    return macro_res

            # 1) Kill switch
            ks_result = await self._check_kill_switch(session)
            if ks_result:
                await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, ks_result, proposed_size)
                return ks_result

            # 2) Daily loss limit
            daily_result = await self._check_daily_loss_limit(session)
            if daily_result:
                await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, daily_result, proposed_size)
                return daily_result

            # 3) Max drawdown
            dd_result = await self._check_max_drawdown(session)
            if dd_result:
                await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, dd_result, proposed_size)
                return dd_result

            # 4) Circuit breaker / volatility
            cb_result = await self._check_circuit_breaker(signal_price, atr)
            if cb_result:
                await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, cb_result, proposed_size)
                return cb_result

            # 5) Position sizing + mandatory stop loss
            sizing_result = await self._calculate_position_size(
                session, symbol, signal_type, signal_price, atr, strategy_stop_loss, proposed_size, trailing_stop
            )
            # sizing_result is APPROVED (tentative) or REJECTED
            if sizing_result.decision == RiskDecision.REJECTED:
                await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, sizing_result, proposed_size)
                return sizing_result

            # 5b) Sentiment filter (Fase 5) - solo reduce/veta, nunca genera
            if sentiment_result is not None:
                if sentiment_result.action == "veto":
                    veto_res = RiskResult(
                        decision=RiskDecision.REJECTED,
                        rule=RiskRule.SENTIMENT_VETO,
                        reason=f"Sentiment veto: {sentiment_result.reason}",
                        approved_size=Decimal("0"),
                    )
                    await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, veto_res, proposed_size)
                    return veto_res
                elif sentiment_result.action == "reduce":
                    # Reduce size by factor
                    factor = Decimal(str(sentiment_result.reduce_factor))
                    reduced = (sizing_result.approved_size * factor).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                    min_notional = Decimal("10")
                    if reduced * signal_price < min_notional:
                        veto2 = RiskResult(
                            decision=RiskDecision.REJECTED,
                            rule=RiskRule.SENTIMENT_VETO,
                            reason=f"Sentiment reduce would push below min notional: {sentiment_result.reason}",
                            approved_size=Decimal("0"),
                        )
                        await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, veto2, proposed_size)
                        return veto2
                    sizing_result.approved_size = reduced
                    sizing_result.decision = RiskDecision.REDUCED
                    sizing_result.rule = RiskRule.SENTIMENT_REDUCE
                    sizing_result.reason = f"Sentiment reduce {int((1-float(factor))*100)}%: {sentiment_result.reason}"

            # 6) Total exposure
            exposure_result = await self._check_total_exposure(session, signal_price, sizing_result.approved_size)
            if exposure_result:
                # Either REDUCED or REJECTED
                if exposure_result.decision == RiskDecision.REJECTED:
                    await self._log_rejection(session, symbol, signal_type, signal_price, strategy_name, regime, exposure_result, proposed_size)
                    return exposure_result
                # REDUCED: adjust size and continue to approval (keep sizing's stop loss)
                sizing_result.approved_size = exposure_result.approved_size
                # If already REDUCED by sentiment, keep sentiment reason, but also note exposure
                if sizing_result.decision == RiskDecision.REDUCED and sizing_result.rule == RiskRule.SENTIMENT_REDUCE:
                    sizing_result.reason += f" + {exposure_result.reason}"
                else:
                    sizing_result.decision = RiskDecision.REDUCED
                    sizing_result.rule = exposure_result.rule
                    sizing_result.reason = exposure_result.reason

            # All passed -> APPROVED (or REDUCED)
            final = await self._approve_signal(
                session, symbol, signal_type, signal_price, strategy_name, regime,
                sizing_result, proposed_size
            )
            return final

    # ------------------------------------------------------------------
    # Individual rule checks (return RiskResult if FAIL, else None)
    # ------------------------------------------------------------------
    async def _check_kill_switch(self, session) -> Optional[RiskResult]:
        from sqlalchemy import select
        result = await session.execute(select(KillSwitch).where(KillSwitch.is_active == True))  # noqa: E712
        ks = result.scalar_one_or_none()
        if ks:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.KILL_SWITCH,
                reason=f"Kill switch active: {ks.reason or 'Manual activation'} by {ks.activated_by or 'unknown'}",
                approved_size=Decimal("0"),
            )
        return None

    async def _check_daily_loss_limit(self, session) -> Optional[RiskResult]:
        daily = await self._get_or_create_daily_stats(session)
        if daily.is_trading_halted:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.DAILY_LOSS_LIMIT,
                reason=f"Daily trading halted: {daily.halt_reason}",
                approved_size=Decimal("0"),
                account_equity=daily.current_equity,
                daily_pnl=daily.daily_pnl,
            )
        limit = daily.starting_equity * (daily.daily_loss_limit_pct / Decimal("100"))
        # daily_pnl is negative when losing
        if daily.daily_pnl <= -limit:
            daily.is_trading_halted = True
            daily.halt_reason = f"Daily loss limit reached: {daily.daily_pnl_pct:.2f}%"
            await session.flush()
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.DAILY_LOSS_LIMIT,
                reason=f"Daily loss limit reached ({daily.daily_pnl_pct:.2f}%). Trading halted for today.",
                approved_size=Decimal("0"),
                account_equity=daily.current_equity,
                daily_pnl=daily.daily_pnl,
            )
        return None

    async def _check_max_drawdown(self, session) -> Optional[RiskResult]:
        daily = await self._get_or_create_daily_stats(session)
        gs = await self._get_global_state(session)
        max_dd_limit = Decimal(str(self._settings.risk_max_drawdown_pct))
        # Sincroniza global y daily: calcula drawdown actual y actualiza históricos
        if gs.peak_equity and gs.peak_equity > 0:
            current_dd = (gs.peak_equity - daily.current_equity) / gs.peak_equity
            pct = current_dd * Decimal("100")
            if pct > daily.max_drawdown_pct:
                daily.max_drawdown_pct = pct
                await session.flush()
            # También actualiza si current es negativo (recuperación) no reduce histórico, histórico queda
        # Histórico es el que dispara el kill switch (no se resetea solo al recuperar)
        historical_dd = daily.max_drawdown_pct / Decimal("100")
        current_dd = (gs.peak_equity - daily.current_equity) / gs.peak_equity if gs.peak_equity and gs.peak_equity > 0 else historical_dd
        # Usa el máximo de ambos para robustez
        effective_dd = max(historical_dd, current_dd) if current_dd > 0 else historical_dd
        if effective_dd >= max_dd_limit:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.MAX_DRAWDOWN,
                reason=f"Max drawdown exceeded ({effective_dd*100:.2f}% >= {max_dd_limit*100:.2f}%). Manual review required. Bot OFF until explicit reactivation.",
                approved_size=Decimal("0"),
                account_equity=daily.current_equity,
                current_drawdown=effective_dd,
            )
        return None

    async def _check_circuit_breaker(self, signal_price: Decimal, atr: Optional[Decimal]) -> Optional[RiskResult]:
        if atr is None:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.DATA_INTEGRITY,
                reason="ATR unavailable - cannot assess volatility, fail safe reject",
                approved_size=Decimal("0"),
            )
        if atr <= 0 or signal_price <= 0:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.DATA_INTEGRITY,
                reason=f"Invalid ATR or price atr={atr} price={signal_price}",
                approved_size=Decimal("0"),
            )
        atr_pct = atr / signal_price
        threshold = Decimal(str(self._settings.risk_volatility_threshold_pct))
        if atr_pct > threshold:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.CIRCUIT_BREAKER,
                reason=f"Volatility circuit breaker: ATR% {atr_pct*100:.2f}% > threshold {threshold*100:.2f}%",
                approved_size=Decimal("0"),
            )
        return None

    async def _calculate_position_size(
        self,
        session,
        symbol: str,
        signal_type: str,
        signal_price: Decimal,
        atr: Optional[Decimal],
        strategy_stop_loss: Optional[Decimal],
        proposed_size: Optional[Decimal],
        trailing_stop: bool,
    ) -> RiskResult:
        daily = await self._get_or_create_daily_stats(session)
        equity = daily.current_equity
        if equity <= 0:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.DATA_INTEGRITY,
                reason=f"Invalid equity {equity}",
                approved_size=Decimal("0"),
            )
        max_risk_pct = Decimal(str(self._settings.risk_max_position_risk_pct))
        max_risk_amount = equity * max_risk_pct

        # Determine stop loss
        stop_loss_price: Optional[Decimal] = None
        stop_loss_type: Optional[str] = None
        if strategy_stop_loss is not None:
            stop_loss_price = strategy_stop_loss
            stop_loss_type = "trailing" if trailing_stop else "strategy"
        elif atr is not None and atr > 0:
            if signal_type == "BUY":
                stop_loss_price = signal_price - (Decimal("2") * atr)
            else:
                stop_loss_price = signal_price + (Decimal("2") * atr)
            stop_loss_type = "trailing" if trailing_stop else "atr"
        else:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.STOP_LOSS_REQUIRED,
                reason="No stop loss provided and ATR unavailable - every position must have stop loss",
                approved_size=Decimal("0"),
            )

        # Validate stop loss direction
        if signal_type == "BUY":
            risk_per_unit = signal_price - stop_loss_price
        else:
            risk_per_unit = stop_loss_price - signal_price

        if risk_per_unit <= 0:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.STOP_LOSS_REQUIRED,
                reason=f"Invalid stop loss: price {signal_price} stop {stop_loss_price} risk_per_unit {risk_per_unit}",
                approved_size=Decimal("0"),
            )

        max_size_by_risk = (max_risk_amount / risk_per_unit).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if max_size_by_risk <= 0:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.POSITION_SIZING,
                reason="Calculated size 0 - risk too high or equity too low",
                approved_size=Decimal("0"),
            )

        approved_size = max_size_by_risk
        if proposed_size is not None:
            if proposed_size <= 0:
                return RiskResult(
                    decision=RiskDecision.REJECTED,
                    rule=RiskRule.DATA_INTEGRITY,
                    reason=f"Invalid proposed size {proposed_size}",
                    approved_size=Decimal("0"),
                )
            approved_size = min(proposed_size, max_size_by_risk)
            if proposed_size > max_size_by_risk:
                # We reduce rather than reject, but still APPROVED with reduced size
                pass

        min_notional = Decimal("10")
        if approved_size * signal_price < min_notional:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.POSITION_SIZING,
                reason=f"Position notional {approved_size*signal_price:.2f} below minimum ${min_notional}",
                approved_size=Decimal("0"),
            )

        return RiskResult(
            decision=RiskDecision.APPROVED,
            rule=RiskRule.POSITION_SIZING,
            reason="Position sized by ATR risk" if atr else "Position sized",
            approved_size=approved_size,
            stop_loss_price=stop_loss_price,
            stop_loss_type=stop_loss_type,
            risk_pct=max_risk_pct,
            account_equity=equity,
            original_size=proposed_size,
            trailing_stop=trailing_stop,
        )

    async def _check_total_exposure(
        self,
        session,
        signal_price: Decimal,
        approved_size: Decimal,
    ) -> Optional[RiskResult]:
        daily = await self._get_or_create_daily_stats(session)
        max_exposure_pct = Decimal(str(self._settings.risk_max_total_exposure_pct))
        max_exposure = daily.current_equity * max_exposure_pct
        new_exposure = daily.total_exposure + (approved_size * signal_price)
        if new_exposure <= max_exposure:
            return None
        available = max_exposure - daily.total_exposure
        if available <= 0:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.TOTAL_EXPOSURE,
                reason=f"Total exposure limit reached {daily.total_exposure:.2f}/{max_exposure:.2f}",
                approved_size=Decimal("0"),
            )
        reduced_size = (available / signal_price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        min_notional = Decimal("10")
        if reduced_size * signal_price < min_notional:
            return RiskResult(
                decision=RiskDecision.REJECTED,
                rule=RiskRule.TOTAL_EXPOSURE,
                reason="Exposure limit would reduce size below minimum - rejected",
                approved_size=Decimal("0"),
            )
        return RiskResult(
            decision=RiskDecision.REDUCED,
            rule=RiskRule.TOTAL_EXPOSURE,
            reason=f"Position reduced to fit exposure limit: available {available:.2f}/{max_exposure:.2f}",
            approved_size=reduced_size,
        )

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------
    async def _approve_signal(
        self,
        session,
        symbol: str,
        signal_type: str,
        signal_price: Decimal,
        strategy_name: str,
        regime: str,
        sizing_result: RiskResult,
        proposed_size: Optional[Decimal],
    ) -> RiskResult:
        daily = await self._get_or_create_daily_stats(session)
        # If reduced due to exposure, keep REDUCED decision
        decision = sizing_result.decision
        rule = sizing_result.rule
        reason = "Signal approved" if decision == RiskDecision.APPROVED else sizing_result.reason
        await self._log_decision(
            session=session,
            symbol=symbol,
            signal_type=signal_type,
            signal_price=signal_price,
            strategy_name=strategy_name,
            regime=regime,
            decision=decision,
            rule=rule,
            reason=reason,
            original_size=proposed_size,
            approved_size=sizing_result.approved_size,
            risk_pct=sizing_result.risk_pct,
            stop_loss_price=sizing_result.stop_loss_price,
            stop_loss_type=sizing_result.stop_loss_type,
            account_equity=daily.current_equity,
            daily_pnl=daily.daily_pnl,
            total_exposure=daily.total_exposure,
            current_drawdown=daily.max_drawdown_pct / Decimal("100"),
        )
        # Update exposure optimistically (will be corrected on fill)
        daily.total_exposure += sizing_result.approved_size * signal_price
        await session.flush()
        return RiskResult(
            decision=decision,
            rule=rule,
            reason=reason,
            approved_size=sizing_result.approved_size,
            stop_loss_price=sizing_result.stop_loss_price,
            stop_loss_type=sizing_result.stop_loss_type,
            risk_pct=sizing_result.risk_pct,
            account_equity=daily.current_equity,
            daily_pnl=daily.daily_pnl,
            total_exposure=daily.total_exposure,
            current_drawdown=daily.max_drawdown_pct / Decimal("100"),
            original_size=proposed_size,
            trailing_stop=sizing_result.trailing_stop,
        )

    async def _log_rejection(self, session, symbol, signal_type, signal_price, strategy_name, regime, result: RiskResult, proposed_size):
        daily = await self._get_or_create_daily_stats(session)
        await self._log_decision(
            session=session,
            symbol=symbol,
            signal_type=signal_type,
            signal_price=signal_price,
            strategy_name=strategy_name,
            regime=regime,
            decision=result.decision,
            rule=result.rule,
            reason=result.reason,
            original_size=proposed_size,
            approved_size=result.approved_size,
            risk_pct=result.risk_pct,
            stop_loss_price=result.stop_loss_price,
            stop_loss_type=result.stop_loss_type,
            account_equity=daily.current_equity if result.account_equity is None else result.account_equity,
            daily_pnl=daily.daily_pnl if result.daily_pnl is None else result.daily_pnl,
            total_exposure=daily.total_exposure if result.total_exposure is None else result.total_exposure,
            current_drawdown=daily.max_drawdown_pct / Decimal("100") if result.current_drawdown is None else result.current_drawdown,
        )

    async def _reject_and_log(self, symbol, signal_type, signal_price, strategy_name, regime, rule, reason, proposed_size, account_equity, daily_pnl) -> RiskResult:
        """Fail-safe reject outside of session (no DB daily needed) but still try to log."""
        result = RiskResult(decision=RiskDecision.REJECTED, rule=rule, reason=reason, approved_size=Decimal("0"))
        try:
            async with self._db.session() as session:
                daily = await self._get_or_create_daily_stats(session)
                await self._log_decision(
                    session=session,
                    symbol=symbol,
                    signal_type=signal_type,
                    signal_price=signal_price,
                    strategy_name=strategy_name,
                    regime=regime,
                    decision=RiskDecision.REJECTED,
                    rule=rule,
                    reason=reason,
                    original_size=proposed_size,
                    approved_size=Decimal("0"),
                    risk_pct=None,
                    stop_loss_price=None,
                    stop_loss_type=None,
                    account_equity=daily.current_equity,
                    daily_pnl=daily.daily_pnl,
                    total_exposure=daily.total_exposure,
                    current_drawdown=daily.max_drawdown_pct / Decimal("100"),
                )
        except Exception:
            pass
        return result

    async def _price_asset_to_usdt(self, asset: str) -> Optional[Decimal]:
        """Obtiene precio asset/USDT via ticker o última vela en klines. Retorna None si no se puede."""
        upper = asset.upper()
        # Intentar via ccxt ticker
        try:
            from trading_bot.data_collection.client import get_binance_client
            client = get_binance_client()
            ticker = await client.fetch_ticker(f"{upper}/USDT")
            price = ticker.get("last") or ticker.get("close") or ticker.get("price") or ticker.get("lastPrice")
            if price is not None:
                return Decimal(str(price))
        except Exception as e:
            logger.debug(f"fetch_ticker {upper}/USDT failed: {e}")
        # Fallback a última vela en DB
        try:
            from sqlalchemy import select
            from trading_bot.storage.models import Kline
            async with self._db.session() as session:
                result = await session.execute(
                    select(Kline).where(Kline.symbol == f"{upper}/USDT").order_by(Kline.open_time.desc()).limit(1)
                )
                k = result.scalar_one_or_none()
                if k and k.close_price:
                    return Decimal(str(k.close_price))
        except Exception as e:
            logger.debug(f"kline fallback for {upper} failed: {e}")
        logger.warning(f"No se pudo obtener precio para {upper}/USDT - balance no valuado")
        return None

    async def _fetch_initial_equity(self) -> Decimal:
        """Calcula equity total del portfolio (USDT + valor de posiciones) - una sola vez por ciclo, cacheado."""
        # Cache de 5s para evitar dos llamadas redundantes en misma inicialización
        if self._cached_equity is not None and self._cached_at and (datetime.now(timezone.utc) - self._cached_at).total_seconds() < 5:
            return self._cached_equity
        try:
            from trading_bot.data_collection.client import get_binance_client
            client = get_binance_client()
            bal = await client.fetch_balance()
            # Construir dict asset -> total
            asset_totals: dict[str, Decimal] = {}
            for k, v in bal.items():
                if k in ("info", "free", "used", "total", "timestamp", "datetime"):
                    continue
                if isinstance(v, dict) and "total" in v:
                    try:
                        asset_totals[k] = Decimal(str(v["total"]))
                    except:
                        continue
                elif isinstance(v, (int, float, str, Decimal)):
                    try:
                        # Evitar que total sea string vacía
                        if str(v).strip() == "":
                            continue
                        asset_totals[k] = Decimal(str(v))
                    except:
                        continue
            # Merge con bal.get("total") si faltan
            total_dict = bal.get("total", {})
            if isinstance(total_dict, dict):
                for k, v in total_dict.items():
                    if k not in asset_totals and v is not None:
                        try:
                            asset_totals[k] = Decimal(str(v))
                        except:
                            continue
            if not asset_totals:
                # Fallback extra: intentar USDT directo
                total = bal.get("USDT")
                if isinstance(total, dict):
                    total = total.get("total") or total.get("free")
                if total is not None:
                    asset_totals["USDT"] = Decimal(str(total))
            if not asset_totals:
                logger.debug("Balance vacío, fallback 10000")
                return Decimal("10000")
            # Sumar portfolio - SOLO activos gestionados (symbols_list) + stablecoins
            # Evita valuar cientos de dust del faucet (BNB, LTC, TRX, etc.)
            usdt_equity = Decimal("0")
            stablecoins = {"USDT", "BUSD", "USDC", "FDUSD", "TUSD", "DAI", "USDP"}
            # Derivar activos gestionados desde symbols_list (ej. ["BTC/USDT","ETH/USDT"] -> {"BTC","ETH"})
            managed_assets = set()
            try:
                for sym in self._settings.symbols_list:
                    base = sym.split("/")[0].strip().upper()
                    if base and base not in stablecoins:
                        managed_assets.add(base)
            except Exception:
                managed_assets = set()
            for asset, total in list(asset_totals.items()):
                if total is None or total == 0:
                    continue
                upper = asset.upper()
                if upper in stablecoins or upper == "USDT":
                    usdt_equity += total
                elif upper in managed_assets:
                    price = await self._price_asset_to_usdt(upper)
                    if price is None:
                        logger.warning(f"No se pudo valuar {asset} balance {total} - skip, continúa con resto")
                        continue
                    usdt_equity += total * price
                else:
                    # Dust del faucet no gestionado - ignorar por completo (intencional, ver test_mark_to_market)
                    logger.debug(f"Ignorando dust no gestionado {asset} balance {total} (no en symbols_list {managed_assets})")
                    continue
            if usdt_equity > 0:
                self._cached_equity = usdt_equity
                self._cached_at = datetime.now(timezone.utc)
                logger.info(f"Equity total portfolio calculado: {usdt_equity} (balances {asset_totals})")
                return usdt_equity
        except Exception as e:
            logger.debug(f"fetch_balance/portfolio failed, fallback 10000: {e}")
        return Decimal("10000")

    async def _get_global_state(self, session) -> "GlobalRiskState":
        from sqlalchemy import select
        from trading_bot.risk_management.models import GlobalRiskState
        result = await session.execute(select(GlobalRiskState).where(GlobalRiskState.id == 1))
        gs = result.scalar_one_or_none()
        if gs:
            return gs
        # Crear con peak inicial = equity inicial real o último daily
        init_eq = await self._fetch_initial_equity()
        # Si hay daily previo, usar su peak
        from sqlalchemy import select as sel2
        result2 = await session.execute(select(DailyStats).order_by(DailyStats.date.desc()).limit(1))
        last = result2.scalar_one_or_none()
        if last and last.peak_equity and last.peak_equity > init_eq:
            init_eq = last.peak_equity
        gs = GlobalRiskState(id=1, peak_equity=init_eq)
        session.add(gs)
        await session.flush()
        return gs

    async def _get_or_create_daily_stats(self, session) -> DailyStats:
        from sqlalchemy import select, func
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(select(DailyStats).where(func.date(DailyStats.date) == today_start.date()))
        daily = result.scalar_one_or_none()
        if daily:
            return daily
        # Buscar día anterior para continuidad de equity
        result_prev = await session.execute(select(DailyStats).order_by(DailyStats.date.desc()).limit(1))
        prev = result_prev.scalar_one_or_none()
        if prev:
            starting_equity = prev.current_equity
            peak_equity = prev.peak_equity
            # Carry global peak también
            gs = await self._get_global_state(session)
            if gs.peak_equity > peak_equity:
                peak_equity = gs.peak_equity
            max_dd = prev.max_drawdown_pct
        else:
            starting_equity = await self._fetch_initial_equity()
            peak_equity = starting_equity
            max_dd = Decimal("0")
            # Inicializar global
            await self._get_global_state(session)
        daily = DailyStats(
            date=today_start,
            starting_equity=starting_equity,
            current_equity=starting_equity,
            peak_equity=peak_equity,
            max_drawdown_pct=max_dd,
            daily_loss_limit_pct=Decimal(str(self._settings.risk_max_daily_loss_pct * 100)),
            total_exposure=Decimal("0"),
        )
        session.add(daily)
        await session.flush()
        return daily

    async def _log_decision(
        self,
        session,
        symbol: str,
        signal_type: str,
        signal_price: Decimal,
        strategy_name: str,
        regime: str,
        decision: RiskDecision,
        rule: RiskRule,
        reason: str,
        original_size: Optional[Decimal],
        approved_size: Optional[Decimal],
        risk_pct: Optional[Decimal],
        stop_loss_price: Optional[Decimal],
        stop_loss_type: Optional[str],
        account_equity: Decimal,
        daily_pnl: Decimal,
        total_exposure: Decimal,
        current_drawdown: Decimal,
    ) -> None:
        log = RiskLog(
            symbol=symbol,
            signal_type=signal_type,
            signal_price=signal_price,
            strategy_name=strategy_name,
            regime=regime,
            decision=decision,
            triggered_rule=rule,
            reason=reason,
            original_size=original_size,
            approved_size=approved_size,
            risk_pct=risk_pct,
            stop_loss_price=stop_loss_price,
            stop_loss_type=stop_loss_type,
            account_equity=account_equity,
            daily_pnl=daily_pnl,
            total_exposure=total_exposure,
            current_drawdown=current_drawdown,
        )
        session.add(log)
        await session.flush()

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------
    async def update_daily_pnl(self, pnl: Decimal) -> None:
        async with self._db.session() as session:
            daily = await self._get_or_create_daily_stats(session)
            gs = await self._get_global_state(session)
            daily.daily_pnl += pnl
            daily.current_equity += pnl
            if daily.starting_equity != 0:
                daily.daily_pnl_pct = (daily.daily_pnl / daily.starting_equity) * Decimal("100")
            # Actualiza peak diario y global
            if daily.current_equity > daily.peak_equity:
                daily.peak_equity = daily.current_equity
            if daily.current_equity > gs.peak_equity:
                gs.peak_equity = daily.current_equity
            # Drawdown global continuo
            if gs.peak_equity != 0:
                drawdown = (gs.peak_equity - daily.current_equity) / gs.peak_equity * Decimal("100")
                if drawdown > daily.max_drawdown_pct:
                    daily.max_drawdown_pct = drawdown
                # También actualiza gs timestamp
                gs.updated_at = datetime.now(timezone.utc)
            daily.updated_at = datetime.now(timezone.utc)
            await session.flush()

    async def update_exposure(self, delta_notional: Decimal) -> None:
        """Update total exposure (positive on open, negative on close)."""
        async with self._db.session() as session:
            daily = await self._get_or_create_daily_stats(session)
            daily.total_exposure += delta_notional
            if daily.total_exposure < 0:
                daily.total_exposure = Decimal("0")
            await session.flush()

    async def activate_kill_switch(self, activated_by: str, reason: str) -> None:
        async with self._db.session() as session:
            from sqlalchemy import select
            result = await session.execute(select(KillSwitch).where(KillSwitch.id == 1))
            ks = result.scalar_one_or_none()
            if not ks:
                ks = KillSwitch(id=1, is_active=False)
                session.add(ks)
            ks.is_active = True
            ks.activated_by = activated_by
            ks.activated_at = datetime.now(timezone.utc)
            ks.reason = reason
            await session.flush()

    async def deactivate_kill_switch(self) -> None:
        async with self._db.session() as session:
            from sqlalchemy import select
            result = await session.execute(select(KillSwitch).where(KillSwitch.id == 1))
            ks = result.scalar_one_or_none()
            if ks:
                ks.is_active = False
                ks.deactivated_at = datetime.now(timezone.utc)
                await session.flush()

    async def get_risk_status(self) -> dict:
        async with self._db.session() as session:
            from sqlalchemy import select, func
            daily = await self._get_or_create_daily_stats(session)
            result = await session.execute(select(KillSwitch).where(KillSwitch.id == 1))
            ks = result.scalar_one_or_none()
            return {
                "kill_switch_active": ks.is_active if ks else False,
                "daily_pnl": float(daily.daily_pnl),
                "daily_pnl_pct": float(daily.daily_pnl_pct),
                "max_drawdown_pct": float(daily.max_drawdown_pct),
                "is_trading_halted": daily.is_trading_halted,
                "halt_reason": daily.halt_reason,
                "current_equity": float(daily.current_equity),
                "peak_equity": float(daily.peak_equity),
                "total_exposure": float(daily.total_exposure),
            }
