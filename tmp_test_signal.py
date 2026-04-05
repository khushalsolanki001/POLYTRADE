import asyncio
import agent
import os
from dotenv import load_dotenv
import logging

# Setup a fake bot
class FakeBot:
    async def send_message(self, *args, **kwargs):
        print(f"MESSAGE: {args} {kwargs}")

async def test_signal():
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    
    # Start the binance WS to get some prices
    print("Connecting to Binance WS...")
    task = asyncio.create_task(agent._binance_ws_loop())
    
    # Wait for some prices
    for i in range(15):
        await asyncio.sleep(1)
        if len(agent._s.prices) >= 10:
            break
        print(f"Prices count: {len(agent._s.prices)}...")
    
    m_p, edge, raw = agent._compute_signal()
    print(f"Signal: prob={m_p}, edge={edge}, raw={raw}")
    
    # Check if a market is found
    market, mode = await agent._get_target_market()
    print(f"Market search mode: {mode}")
    if market:
        print(f"Found market: {market.get('slug')}")
        age = agent._window_age_secs(market.get('slug'))
        print(f"Window age: {age}s")
    else:
        print("No market found.")
        
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(test_signal())
