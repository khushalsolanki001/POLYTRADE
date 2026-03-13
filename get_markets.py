import asyncio
import aiohttp
import json

async def fetch_active():
    url = "https://gamma-api.polymarket.com/events?active=true&closed=false&limit=5"
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            for e in data:
                print(f"https://polymarket.com/event/{e['slug']}")

if __name__ == "__main__":
    asyncio.run(fetch_active())
