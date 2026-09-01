import asyncio
from trading_bot.data_collection.client import BinanceClient

async def test():
    c = BinanceClient()
    await c.initialize()
    bal = await c._exchange.fetch_balance()
    print(bal)
    await c._exchange.close()

asyncio.run(test())