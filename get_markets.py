import asyncio
import aiohttp
import json

async def fetch_active():
    url = "https://gamma-api.polymarket.com/events?active=true&closed=false&limit=20"
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            for e in data:
                markets = e.get("markets", [])
                if markets:
                    prices = json.loads(markets[0].get("outcomePrices", "[]"))
                    if prices:
                        p = float(prices[0])
                        if p > 0 and p < 1:
                            print(f"https://polymarket.com/event/{e['slug']} has price {p} for {markets[0]['outcomes']}")
                            return

if __name__ == "__main__":
    asyncio.run(fetch_active())
