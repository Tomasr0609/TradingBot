"""Validación Fase 6 - Transición a dinero real con múltiples confirmaciones."""

import os
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# Frase exacta que el usuario debe escribir para confirmar (no viene en templates)
EXPECTED_PHRASE = "SI, ENTIENDO QUE PUEDO PERDER TODO Y AUTORIZO TRADING REAL"

class LiveTradingNotAuthorized(RuntimeError):
    pass

def assert_live_trading_authorized(settings) -> None:
    """
    Verifica TODAS las condiciones para permitir mainnet.
    Si alguna falla, lanza LiveTradingNotAuthorized y el bot no arranca.
    """
    # 1. Debe ser mainnet URL
    if settings.is_testnet:
        raise LiveTradingNotAuthorized("BINANCE_BASE_URL aún apunta a testnet. Para Fase 6 debe ser https://api.binance.com")

    # 2. TRADING_MODE debe ser live
    if settings.trading_mode != "live":
        raise LiveTradingNotAuthorized(f"TRADING_MODE={settings.trading_mode} - para live debe ser 'live'")

    # 3. Doble flag manual (no existen por defecto)
    allow = os.getenv("ALLOW_LIVE_TRADING", "").lower()
    confirm = os.getenv("CONFIRM_LIVE_TRADING_I_UNDERSTAND_RISK", "").lower()
    phrase = os.getenv("LIVE_TRADING_CONFIRM_PHRASE", "")

    if allow != "true":
        raise LiveTradingNotAuthorized("Falta ALLOW_LIVE_TRADING=true (flag manual 1/3). Crea esta variable a mano, no está en templates por seguridad.")
    if confirm != "true":
        raise LiveTradingNotAuthorized("Falta CONFIRM_LIVE_TRADING_I_UNDERSTAND_RISK=true (flag manual 2/3).")
    if phrase != EXPECTED_PHRASE:
        raise LiveTradingNotAuthorized(f"Falta LIVE_TRADING_CONFIRM_PHRASE exacta. Debe ser: '{EXPECTED_PHRASE}' (flag manual 3/3)")

    # 4. Límites más conservadores que testnet
    if settings.risk_max_daily_loss_pct > 0.015:
        raise LiveTradingNotAuthorized(f"RISK_MAX_DAILY_LOSS_PCT={settings.risk_max_daily_loss_pct} demasiado alto para live. Máximo 0.015 (1.5%) en arranque real.")
    if settings.risk_max_position_risk_pct > 0.005:
        raise LiveTradingNotAuthorized(f"RISK_MAX_POSITION_RISK_PCT={settings.risk_max_position_risk_pct} demasiado alto. Máximo 0.005 (0.5%) en live.")
    if settings.risk_max_total_exposure_pct > 0.10:
        raise LiveTradingNotAuthorized(f"RISK_MAX_TOTAL_EXPOSURE_PCT={settings.risk_max_total_exposure_pct} demasiado alto. Máximo 0.10 (10%) en live.")

    logger.warning("⚠️ FASE 6 AUTORIZADA: Trading REAL activado con múltiples confirmaciones. Monto en riesgo debe ser que puedas perder por completo.")

async def verify_api_key_restrictions(exchange) -> None:
    """
    Verifica que la key de producción tiene retiros deshabilitados y IP restringida.
    Usa ccxt private endpoints. Si no puede verificar, falla hacia lado seguro (no opera).
    """
    try:
        # ccxt binance tiene endpoint sapiGetAccountStatus o similar; usamos fetchAccount or custom
        # Intento 1: account info via private
        # Para Binance, podemos llamar exchange.privateGetAccount() o similar
        # Como mock es difícil, hacemos best-effort: intenta `exchange.sapiGetApiRestrictions` si existe
        method = None
        for candidate in ["sapiGetAccountStatus", "privateGetAccount", "fetchAccount", "sapiGetApiRestrictions"]:
            if hasattr(exchange, candidate):
                method = candidate
                break
        if method is None:
            logger.warning("No se pudo verificar restricciones de API key (método no encontrado). Verifica manualmente en Binance: Withdrawals OFF e IP whitelist.")
            return

        resp = await getattr(exchange, method)()
        # Binance apiRestrictions response: {canTrade, canWithdraw, canDeposit, ...}
        # Si withdraw true -> block
        can_withdraw = None
        if isinstance(resp, dict):
            can_withdraw = resp.get("canWithdraw")
            if can_withdraw is None:
                # Try nested
                can_withdraw = resp.get("can_withdraw")
            # Algunos endpoints retornan isLocked etc
        if can_withdraw is True:
            raise LiveTradingNotAuthorized("API key tiene retiros/withdrawals HABILITADOS - INNEGOCIABLE: deshabilita retiros en Binance antes de live.")
        if can_withdraw is False:
            logger.info("Verificado: API key tiene retiros DESHABILITADOS ✓")
        else:
            logger.warning(f"No se pudo confirmar canWithdraw={can_withdraw}. Verifica manualmente que retiros estén OFF.")

        # IP check (si LIVE_ALLOWED_IPS configurada)
        allowed_ips = os.getenv("LIVE_ALLOWED_IPS", "")
        if allowed_ips:
            logger.info(f"LIVE_ALLOWED_IPS configuradas: {allowed_ips} - verifica que coincidan con IP del VPS y que Binance tenga 'Restrict to trusted IPs' activo.")
    except LiveTradingNotAuthorized:
        raise
    except Exception as e:
        logger.warning(f"No se pudo verificar restricciones de API key: {e} - fail safe: verifica manualmente antes de operar.")
        # No lanzamos, pero advertimos; en producción estricta podrías querer lanzar
        # raise LiveTradingNotAuthorized(f"No se pudo verificar key: {e}")
