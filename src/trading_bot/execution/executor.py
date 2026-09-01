"""Order executor - NEVER executes without risk gateway. Fase 4: Testnet real via ccxt."""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from trading_bot.risk_management.engine import RiskEngine, RiskResult
from trading_bot.risk_management.models import RiskDecision

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    symbol: str
    signal_type: str  # BUY / SELL
    signal_price: Decimal
    strategy_name: str
    regime: str
    atr: Optional[Decimal] = None
    strategy_stop_loss: Optional[Decimal] = None
    proposed_size: Optional[Decimal] = None
    trailing_stop: bool = False
    risk_result: Optional[RiskResult] = None
    sentiment_result: Optional[object] = None  # SentimentFilterResult Fase 5
    macro_events: Optional[list] = None


@dataclass
class ExecutionResult:
    executed: bool
    reason: str
    order_id: Optional[str] = None
    executed_size: Optional[Decimal] = None
    executed_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    raw_response: Optional[dict] = None
    stop_loss_order_id: Optional[str] = None
    has_protection: bool = False


class RiskGatewayError(RuntimeError):
    pass


class TestnetEnforcementError(RuntimeError):
    """Raised if mainnet URL/key detected."""
    pass


class OrderExecutor:
    """
    Executes orders ONLY if they have passed through RiskEngine.
    Validación estricta en código (no solo config) de que es Testnet.
    Registra todo en executed_orders. Después de cada entrada coloca stop loss real.
    """

    def __init__(self, risk_engine: Optional[RiskEngine] = None, exchange_client=None, telegram_notifier=None):
        self._risk = risk_engine or RiskEngine()
        self._exchange = exchange_client
        self._telegram = telegram_notifier
        self._executed_orders: list[OrderRequest] = []

    def _assert_testnet(self) -> None:
        from trading_bot.config.settings import get_settings
        settings = get_settings()
        # Chequeo en código, no solo config: URL debe ser testnet
        if not settings.is_testnet:
            raise TestnetEnforcementError(
                f"Refusing execution - BINANCE_BASE_URL is not testnet: {settings.binance_base_url}. "
                "Fase 4 SOLO permite Testnet. Para mainnet requiere autorización explícita Fase 6."
            )
        # Si exchange client tiene urls, verificar
        if self._exchange and hasattr(self._exchange, "urls"):
            urls_str = str(getattr(self._exchange, "urls", ""))
            if "testnet" not in urls_str.lower() and "testnet.binance.vision" not in settings.binance_base_url:
                raise TestnetEnforcementError(f"Exchange client not pointing to testnet: {urls_str}")

    async def execute(self, req: OrderRequest) -> ExecutionResult:
        if req.risk_result is None:
            raise RiskGatewayError("Order without risk_result - blocked. Must call RiskEngine.evaluate_signal first.")
        if req.risk_result.decision == RiskDecision.REJECTED:
            # Log rejection in DB
            await self._log_executed_order(req, status="rejected", error_message=f"Blocked by risk: {req.risk_result.reason}")
            return ExecutionResult(executed=False, reason=f"Blocked by risk: {req.risk_result.reason} [{req.risk_result.rule.value}]")
        if req.risk_result.decision not in (RiskDecision.APPROVED, RiskDecision.REDUCED):
            raise RiskGatewayError(f"Unknown risk decision {req.risk_result.decision}")
        if req.risk_result.stop_loss_price is None:
            raise RiskGatewayError("Approved risk result missing stop loss")

        # Validación Testnet estricta en código
        self._assert_testnet()

        # Paper mode (sin exchange) - para tests y modo paper
        if self._exchange is None:
            self._executed_orders.append(req)
            result = ExecutionResult(
                executed=True,
                reason="paper execution (no exchange) - testnet validated",
                order_id=f"paper-{len(self._executed_orders)}",
                executed_size=req.risk_result.approved_size,
                executed_price=req.signal_price,
                stop_loss=req.risk_result.stop_loss_price,
            )
            # Simular colocación de stop loss también en paper
            sl_order_id = f"paper-sl-{len(self._executed_orders)}"
            result.stop_loss_order_id = sl_order_id
            result.has_protection = True
            await self._log_executed_order(req, status="filled", executed_price=req.signal_price, executed_size=req.risk_result.approved_size, order_id=result.order_id, raw_response={"paper": True}, stop_loss_order_id=sl_order_id, has_protection=True)
            return result

        # Real Testnet execution via ccxt
        try:
            side = req.signal_type.lower()
            amount = float(req.risk_result.approved_size)
            logger.info(f"Submitting TESTNET order {req.symbol} {side} {amount} @ market (signal {req.signal_price}) SL {req.risk_result.stop_loss_price}")
            order = await self._exchange.create_order(req.symbol, "market", side, amount)
            order_id = str(order.get("id", "unknown"))
            executed_price = Decimal(str(order.get("price") or order.get("average") or req.signal_price))
            executed_size = Decimal(str(order.get("filled") or order.get("amount") or amount))
            fee = order.get("fee", {})
            # Intentar colocar stop loss real inmediatamente
            sl_order_id = None
            has_protection = False
            sl_error = None
            try:
                sl_side = "sell" if side == "buy" else "buy"
                sl_price = float(req.risk_result.stop_loss_price)
                # Binance spot: STOP_LOSS_LIMIT requiere stopPrice y price
                # Intentamos con ccxt: type stop_loss_limit
                sl_order = None
                # Try stop_loss_limit first, fallback to stop
                try:
                    sl_order = await self._exchange.create_order(req.symbol, "stop_loss_limit", sl_side, amount, sl_price, {"stopPrice": sl_price})
                except Exception as e1:
                    logger.warning(f"stop_loss_limit failed, trying STOP_LOSS: {e1}")
                    sl_order = await self._exchange.create_order(req.symbol, "stop_loss", sl_side, amount, None, {"stopPrice": sl_price})
                sl_order_id = str(sl_order.get("id", "unknown-sl"))
                has_protection = True
                logger.info(f"Stop loss placed {req.symbol} {sl_side} {amount} @ {sl_price} id={sl_order_id}")
            except Exception as sl_e:
                sl_error = str(sl_e)
                logger.critical(f"CRITICAL: entrada {order_id} ejecutada pero stop loss FALLÓ {req.symbol}: {sl_e} - posición SIN PROTECCIÓN")
                # Notificar Telegram con máxima prioridad
                if self._telegram:
                    try:
                        await self._telegram.notify_unprotected_position(req.symbol, str(req.risk_result.approved_size), float(req.risk_result.stop_loss_price), sl_error)
                    except Exception as e2:
                        logger.error(f"Failed to notify unprotected position: {e2}")
                has_protection = False

            status = "filled" if has_protection else "unprotected"
            error_msg = sl_error if not has_protection else None
            await self._log_executed_order(req, status=status, order_id=order_id, executed_price=executed_price, executed_size=executed_size, fee=Decimal(str(fee.get("cost", 0))) if fee else None, raw_response=order, order_type="market", requested_size=req.risk_result.approved_size, stop_loss_order_id=sl_order_id, has_protection=has_protection, error_message=error_msg)
            return ExecutionResult(
                executed=True,
                reason="testnet order filled" + ("" if has_protection else " - WITHOUT PROTECTION"),
                order_id=order_id,
                executed_size=executed_size,
                executed_price=executed_price,
                stop_loss=req.risk_result.stop_loss_price,
                raw_response=order,
                stop_loss_order_id=sl_order_id,
                has_protection=has_protection,
            )
        except Exception as e:
            logger.error(f"Testnet order failed {req.symbol}: {e}")
            await self._log_executed_order(req, status="error", error_message=str(e), raw_response={"error": str(e)}, has_protection=False)
            return ExecutionResult(executed=False, reason=f"Exchange error: {e}", has_protection=False)

    async def execute_via_risk(self, req: OrderRequest) -> ExecutionResult:
        risk_result = await self._risk.evaluate_signal(
            symbol=req.symbol,
            signal_type=req.signal_type,
            signal_price=req.signal_price,
            strategy_name=req.strategy_name,
            regime=req.regime,
            atr=req.atr,
            strategy_stop_loss=req.strategy_stop_loss,
            proposed_size=req.proposed_size,
            trailing_stop=req.trailing_stop,
            sentiment_result=req.sentiment_result,
            macro_events=req.macro_events,
        )
        req.risk_result = risk_result
        return await self.execute(req)

    async def _log_executed_order(self, req: OrderRequest, status: str, order_id=None, executed_price=None, executed_size=None, fee=None, raw_response=None, order_type=None, requested_size=None, error_message=None, stop_loss_order_id=None, has_protection=False):
        """Persiste auditoría completa en DB (executed_orders)."""
        from trading_bot.storage.database import get_database
        from trading_bot.execution.models import ExecutedOrder
        from trading_bot.config.settings import get_settings
        db = get_database()
        settings = get_settings()
        try:
            async with db.session() as session:
                rec = ExecutedOrder(
                    symbol=req.symbol,
                    signal_type=req.signal_type,
                    signal_price=req.signal_price,
                    strategy_name=req.strategy_name,
                    regime=req.regime,
                    atr=req.atr,
                    risk_decision=req.risk_result.decision.value if req.risk_result else "unknown",
                    risk_rule=req.risk_result.rule.value if req.risk_result else "unknown",
                    risk_reason=req.risk_result.reason if req.risk_result else "no risk result",
                    approved_size=req.risk_result.approved_size if req.risk_result else None,
                    stop_loss_price=req.risk_result.stop_loss_price if req.risk_result else None,
                    trailing_stop=req.trailing_stop,
                    order_id=order_id,
                    order_type=order_type or "market",
                    requested_size=requested_size if requested_size is not None else (req.risk_result.approved_size if req.risk_result else None),
                    status=status,
                    executed_price=executed_price,
                    executed_size=executed_size,
                    fee=fee,
                    raw_response=json.dumps(raw_response, default=str) if raw_response else None,
                    error_message=error_message,
                    is_testnet=settings.is_testnet,
                    stop_loss_order_id=stop_loss_order_id,
                    has_protection=has_protection,
                )
                session.add(rec)
                await session.flush()
        except Exception as e:
            logger.error(f"Failed to log executed_order: {e}")

    @property
    def executed_count(self) -> int:
        return len(self._executed_orders)
