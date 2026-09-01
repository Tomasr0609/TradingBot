"""Telegram notifications and kill switch integration."""

import asyncio
import logging
from typing import Optional, Callable
from dataclasses import dataclass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from trading_bot.config.settings import get_settings
from trading_bot.risk_management.engine import RiskEngine

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    allowed_users: list[int]  # List of user IDs allowed to control the bot


class TelegramNotifier:
    """Handles Telegram notifications and remote kill switch."""

    def __init__(self, risk_engine: RiskEngine) -> None:
        self._risk_engine = risk_engine
        self._settings = get_settings()
        self._application: Optional[Application] = None
        self._config: Optional[TelegramConfig] = None
        self._running = False

    def configure(self, bot_token: str, chat_id: str, allowed_users: list[int] = None) -> None:
        """Configure Telegram bot credentials."""
        self._config = TelegramConfig(
            bot_token=bot_token,
            chat_id=chat_id,
            allowed_users=allowed_users or [],
        )

    async def start(self) -> None:
        """Start the Telegram bot."""
        if not self._config:
            logger.warning("Telegram not configured, skipping start")
            return

        self._application = Application.builder().token(self._config.bot_token).build()

        # Add handlers
        self._application.add_handler(CommandHandler("start", self._cmd_start))
        self._application.add_handler(CommandHandler("status", self._cmd_status))
        self._application.add_handler(CommandHandler("kill", self._cmd_kill))
        self._application.add_handler(CommandHandler("unkill", self._cmd_unkill))
        self._application.add_handler(CommandHandler("help", self._cmd_help))
        self._application.add_handler(CallbackQueryHandler(self._callback_query))

        await self._application.initialize()
        await self._application.start()
        await self._application.updater.start_polling()
        self._running = True
        logger.info("Telegram bot started")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self._application and self._running:
            await self._application.updater.stop()
            await self._application.stop()
            await self._application.shutdown()
            self._running = False
            logger.info("Telegram bot stopped")

    def _check_authorized(self, user_id: int) -> bool:
        """Check if user is authorized to control the bot."""
        if not self._config:
            return False
        if not self._config.allowed_users:
            return True  # No restrictions if not configured
        return user_id in self._config.allowed_users

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("❌ No autorizado")
            return
        
        await update.message.reply_text(
            "🤖 Trading Bot Activo\n"
            "Comandos disponibles:\n"
            "/status - Estado del bot\n"
            "/kill - Activar kill switch\n"
            "/unkill - Desactivar kill switch\n"
            "/help - Esta ayuda"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("❌ No autorizado")
            return
        await self._cmd_start(update, context)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command - show risk status."""
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("❌ No autorizado")
            return

        status = await self._risk_engine.get_risk_status()
        
        kill_emoji = "🔴" if status["kill_switch_active"] else "🟢"
        halt_emoji = "🛑" if status["is_trading_halted"] else "✅"
        
        msg = (
            f"{kill_emoji} <b>Kill Switch:</b> {'ACTIVO' if status['kill_switch_active'] else 'Inactivo'}\n"
            f"{halt_emoji} <b>Trading:</b> {'DETENIDO' if status['is_trading_halted'] else 'Activo'}\n\n"
            f"💰 <b>Capital:</b> ${status['current_equity']:,.2f}\n"
            f"📈 <b>Peak:</b> ${status['peak_equity']:,.2f}\n"
            f"📊 <b>PnL Diario:</b> ${status['daily_pnl']:,.2f} ({status['daily_pnl_pct']:.2f}%)\n"
            f"📉 <b>Max Drawdown:</b> {status['max_drawdown_pct']:.2f}%\n"
        )
        
        if status["halt_reason"]:
            msg += f"\n⚠️ <b>Motivo parada:</b> {status['halt_reason']}"
        
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /kill command - activate kill switch."""
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("❌ No autorizado")
            return

        # Ask for confirmation
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirmar KILL SWITCH", callback_data="confirm_kill"),
                InlineKeyboardButton("❌ Cancelar", callback_data="cancel_kill"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚠️ <b>¿CONFIRMAS ACTIVAR KILL SWITCH?</b>\n\n"
            "Esto detendrá TODA la actividad de trading inmediatamente.\n"
            "El bot no abrirá nuevas posiciones hasta que se desactive con /unkill.",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _cmd_unkill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /unkill command - deactivate kill switch."""
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("❌ No autorizado")
            return

        await self._risk_engine.deactivate_kill_switch()
        await update.message.reply_text("✅ Kill switch DESACTIVADO. Trading reanudado.")

    async def _callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard callbacks."""
        query = update.callback_query
        await query.answer()
        
        if not self._check_authorized(query.from_user.id):
            await query.edit_message_text("❌ No autorizado")
            return

        if query.data == "confirm_kill":
            await self._risk_engine.activate_kill_switch("telegram", f"Activado por {query.from_user.username or query.from_user.id} via Telegram")
            await query.edit_message_text("🔴 <b>KILL SWITCH ACTIVADO</b>\n\nTrading detenido completamente.", parse_mode="HTML")
        elif query.data == "cancel_kill":
            await query.edit_message_text("✅ Kill switch cancelado. Trading continúa.")

    async def notify_trade_executed(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        stop_loss: float = None,
    ) -> None:
        """Notify trade execution."""
        if not self._config or not self._running:
            return
        
        emoji = "🟢" if side == "BUY" else "🔴"
        msg = (
            f"{emoji} <b>Orden Ejecutada</b>\n"
            f"Par: {symbol}\n"
            f"Lado: {side}\n"
            f"Tamaño: {size:.6f}\n"
            f"Precio: ${price:,.2f}"
        )
        if stop_loss:
            msg += f"\nStop Loss: ${stop_loss:,.2f}"
        
        try:
            await self._application.bot.send_message(
                chat_id=self._config.chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send trade notification: {e}")

    async def notify_risk_rejection(self, symbol: str, reason: str, rule: str) -> None:
        """Notify signal rejection by risk management."""
        if not self._config or not self._running:
            return
        
        msg = (
            f"🛑 <b>Señal Rechazada por Riesgo</b>\n"
            f"Par: {symbol}\n"
            f"Regla: {rule}\n"
            f"Motivo: {reason}"
        )
        
        try:
            await self._application.bot.send_message(
                chat_id=self._config.chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send risk notification: {e}")

    async def notify_daily_limit_reached(self, pnl_pct: float) -> None:
        """Notify daily loss limit reached."""
        if not self._config or not self._running:
            return
        
        msg = (
            f"🚨 <b>LÍMITE DIARIO ALCANZADO</b>\n"
            f"Pérdida diaria: {pnl_pct:.2f}%\n"
            f"Trading detenido para el resto del día."
        )
        
        try:
            await self._application.bot.send_message(
                chat_id=self._config.chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send daily limit notification: {e}")

    async def notify_circuit_breaker(self, symbol: str, atr_pct: float, threshold: float) -> None:
        """Notify circuit breaker activation."""
        if not self._config or not self._running:
            return
        
        msg = (
            f"⚡ <b>CIRCUIT BREAKER ACTIVADO</b>\n"
            f"Par: {symbol}\n"
            f"Volatilidad (ATR%): {atr_pct:.2f}%\n"
            f"Umbral: {threshold:.2f}%\n"
            f"Trading pausado hasta que la volatilidad disminuya."
        )
        
        try:
            await self._application.bot.send_message(
                chat_id=self._config.chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send circuit breaker notification: {e}")

    async def notify_max_drawdown(self, drawdown_pct: float, limit_pct: float) -> None:
        """Notify max drawdown exceeded."""
        if not self._config or not self._running:
            return
        
        msg = (
            f"🚨 <b>MAX DRAWDOWN EXCEDIDO</b>\n"
            f"Drawdown actual: {drawdown_pct:.2f}%\n"
            f"Límite: {limit_pct:.2f}%\n"
            f"Bot detenido completamente. Revisión manual requerida."
        )
        
        try:
            await self._application.bot.send_message(
                chat_id=self._config.chat_id,
                text=msg,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send max drawdown notification: {e}")

    async def notify_unprotected_position(self, symbol: str, size: str, stop_price: float, error: str) -> None:
        """CRITICAL: entrada ejecutada pero stop loss falló - posición sin protección."""
        # Intenta enviar incluso si _running es False (fail-safe: usa bot si hay config)
        if not self._config:
            logger.critical(f"UNPROTECTED POSITION {symbol} size {size} SL {stop_price} error {error} - NO TELEGRAM CONFIG")
            return
        msg = (
            f"🚨🚨 <b>POSICIÓN SIN PROTECCIÓN - CRÍTICO</b> 🚨🚨\n"
            f"Par: {symbol}\n"
            f"Tamaño: {size}\n"
            f"Stop Loss deseado: ${stop_price:.2f}\n"
            f"Error: {error}\n"
            f"Acción: Verifica en Binance y coloca stop manual INMEDIATAMENTE."
        )
        try:
            if self._application and self._running:
                await self._application.bot.send_message(chat_id=self._config.chat_id, text=msg, parse_mode="HTML")
            else:
                # Fallback: intenta crear bot temporal
                from telegram import Bot
                bot = Bot(token=self._config.bot_token)
                await bot.send_message(chat_id=self._config.chat_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send unprotected notification: {e}")


# Global notifier instance
_notifier_instance: Optional[TelegramNotifier] = None


def get_telegram_notifier(risk_engine: RiskEngine = None) -> TelegramNotifier:
    """Get or create global Telegram notifier."""
    global _notifier_instance
    if _notifier_instance is None:
        if risk_engine is None:
            risk_engine = RiskEngine()
        _notifier_instance = TelegramNotifier(risk_engine)
    return _notifier_instance