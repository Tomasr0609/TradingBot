#!/usr/bin/env python
"""Entrypoint para Fase 4 - Paper/Testnet bot 24/7. Portable Windows/Linux/Mac."""
import asyncio
import logging
import signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from trading_bot.bot import TradingBot

async def main():
    bot = TradingBot()
    await bot.initialize()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    def _handler(*_):
        stop.set()
    # Señales para Linux/Mac (prolijo)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            pass  # Windows no implementa add_signal_handler, se cubre con KeyboardInterrupt abajo

    task = asyncio.create_task(bot.run_forever())
    # Fuente principal para Windows es KeyboardInterrupt (no add_signal_handler)
    try:
        await stop.wait()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received - initiating graceful shutdown (Windows compatible)")
        stop.set()
    # Camino de apagado prolijo compartido
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await bot.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Última red de seguridad si la interrupción ocurre fuera de main()
        # asyncio.run ya maneja CancelledError, pero este bloque evita traceback sin manejar
        pass
