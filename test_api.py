import asyncio
import api
async def test():
    trades = await api.fetch_trades("0xd2d75a43ba5addf54b4194c7b8aa6db8a2b5e28a", limit=1)
    if trades:
        print(trades)
    else:
        print("no trades found for that generic wallet")
asyncio.run(test())
