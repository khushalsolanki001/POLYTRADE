import asyncio
import aiohttp
import json

async def fetch_event_by_slug(slug):
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return data

async def main():
    slug = "btc-updown-5m-1773390900" 
    data = await fetch_event_by_slug(slug)
    print(json.dumps(data, indent=2))

asyncio.run(main())
