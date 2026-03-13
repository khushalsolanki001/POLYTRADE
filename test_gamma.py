import asyncio
import aiohttp

async def fetch_market_by_asset(asset_id):
    url = f"https://gamma-api.polymarket.com/events?asset_id={asset_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data and isinstance(data, list):
                event = data[0]
                return event
    return None

async def main():
    print(await fetch_market_by_asset(102928892403565256221775619525048924719226871059521366304445831518972551068883))

asyncio.run(main())
