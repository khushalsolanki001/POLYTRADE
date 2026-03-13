import asyncio
from web3 import Web3
from sys import argv
import aiohttp

w3 = Web3(Web3.HTTPProvider('https://polygon.drpc.org'))

async def fetch_market_by_asset(asset_id, amount_of_shares):
    # asset_id is the token_id
    url = f"https://gamma-api.polymarket.com/markets?asset_id={asset_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data and isinstance(data, list):
                market = data[0]
                tokens = market.get('tokens', [])
                outcome = "?"
                for idx, t in enumerate(tokens):
                    if t.get('token_id') == str(asset_id):
                        outcome = market['outcomes'][idx] if 'outcomes' in market else "Yes/No?"
                return {
                    "title": market.get('title', 'Unknown Market'),
                    "outcome": outcome,
                    "conditionId": market.get('conditionId', ''),
                }
    return None

async def test_tx(tx_hash):
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    
    # USDC Contract
    USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174".lower()
    CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045".lower()
    
    usdc_transferred = 0.0
    ctf_transfers = []
    
    for log in receipt['logs']:
        addr = log['address'].lower()
        if addr == USDC:
            # Transfer(address from, address to, uint256 value)
            if log['topics'][0].hex() == '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef':
                val = int.from_bytes(log['data'], 'big') / 1e6
                usdc_transferred += val
        elif addr == CTF:
            if log['topics'][0].hex() == '0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62':
                # TransferSingle
                token_id = int.from_bytes(log['data'][:32], 'big')
                shares = int.from_bytes(log['data'][32:], 'big') / 1e6
                _to = '0x' + log['topics'][3].hex()[-40:]
                ctf_transfers.append({"token_id": token_id, "shares": shares, "to": _to})
                
    print(f"Total USDC in TX: {usdc_transferred}")
    
    for t in ctf_transfers:
        print(f"CTF Transfer: {t['shares']} shares of {t['token_id']} to {t['to']}")
        info = await fetch_market_by_asset(t['token_id'], t['shares'])
        if info:
            print(f"Market: {info['title']}, Outcome: {info['outcome']}")

if __name__ == '__main__':
    asyncio.run(test_tx('0x86ead7e0c048f06e9de11f04d48aef90528d33ae21ba682b14c9bd5ae1fd5783'))
