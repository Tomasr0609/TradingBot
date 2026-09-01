#!/usr/bin/env python
"""Entrypoint para Fase 4 - Paper/Testnet bot 24/7."""
import asyncio
import logging
import signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from trading_bot.bot import TradingBot

async def main():
    bot = TradingBot()
    await bot.initialize()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    def _handler(*_):
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            pass  # Windows

    task = asyncio.create_task(bot.run_forever())
    # Also allow single iteration mode via env?
    await stop.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await bot.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
