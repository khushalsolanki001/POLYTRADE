import asyncio
import logging
from web3 import Web3
from web3.exceptions import Web3Exception
import aiohttp
from datetime import datetime, timezone
import json

from db import get_all_wallets, update_last_timestamp
from db import get_all_wallets, update_last_timestamp
from handlers import format_trade_alert
from api import fetch_trades, get_trade_title, parse_trade_outcome, parse_trade_price

logger = logging.getLogger(__name__)

RPC_URL = "https://polygon.drpc.org"
def get_w3():
    return Web3(Web3.HTTPProvider(RPC_URL))

CTF_CONTRACT = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

_asset_cache = {}

async def fetch_market_info(asset_id: str, address: str = ""):
    """Fetch market title and outcome. First checks cache, then falls back to recent trades."""
    if asset_id in _asset_cache:
        return _asset_cache[asset_id]
        
    # Fallback to fetching recent trades of this user to find the title
    if address:
        try:
            trades = await fetch_trades(address, limit=30)
            for t in trades:
                if str(t.get('asset')) == str(asset_id) or str(t.get('conditionId')) == str(asset_id):
                    title = get_trade_title(t) or 'Unknown Market'
                    outcome = parse_trade_outcome(t) or '?'
                    price = parse_trade_price(t) or 0.5
                    
                    result = {
                        "title": title,
                        "outcome": outcome,
                        "price": price
                    }
                    _asset_cache[asset_id] = result
                    return result
        except Exception as e:
            logger.error(f"Fallback fetch_trades error for {asset_id}: {e}")
            
    return {"title": "Unknown Market", "outcome": "?", "price": 0.5}

async def run_market_cacher():
    """Continuously fetches all active markets from CLOB API to populate _asset_cache."""
    logger.info("🚀 Starting background Token Cacher...")
    while True:
        try:
            url = "https://clob.polymarket.com/markets?active=true"
            async with aiohttp.ClientSession() as session:
                while url:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                        markets = data.get('data', [])
                        if not markets:
                            break
                        
                        for m in markets:
                            title = m.get('question') or m.get('title') or 'Unknown Market'
                            for t in m.get('tokens', []):
                                t_id = str(t.get('token_id', ''))
                                if t_id:
                                    _asset_cache[t_id] = {
                                        "title": title,
                                        "outcome": str(t.get('outcome', '?')),
                                        "price": float(t.get('price', 0.5))
                                    }
                                    
                        next_cursor = data.get('next_cursor')
                        if not next_cursor or next_cursor == "Mw==":
                            break
                        url = f"https://clob.polymarket.com/markets?active=true&next_cursor={next_cursor}"
                        
            # Sleep 60s before re-syncing to avoid slamming CLOB
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Token cacher error: {e}")
            await asyncio.sleep(15)

async def run_block_scanner(app):
    """Background task to scan blocks every 3 seconds for instant alerts."""
    
    # Start the token cacher alongside scanning
    asyncio.create_task(run_market_cacher())
    
    w3 = get_w3()
    
    # We start searching from 5 blocks behind current to catch up
    try:
        start_block = w3.eth.block_number - 5
    except Exception as e:
        logger.error(f"Failed to get initial block number: {e}")
        start_block = 0
        
    logger.info(f"🚀 Block scanner started from block {start_block}")
    
    while True:
        try:
            latest_block = w3.eth.block_number
            if start_block > latest_block:
                await asyncio.sleep(2)
                continue
            
            # Re-fetch wallets every loop so if users add/drop we catch it
            wallets_rows = get_all_wallets()
            if not wallets_rows:
                # No one watching anything, just move cursor forward and chill
                start_block = latest_block + 1
                await asyncio.sleep(5)
                continue
                
            # Build quick-lookup dictionary for watched addresses
            # Use lowercase for dict keys just in case
            tracked_wallets = {}
            for row in wallets_rows:
                addr = row['wallet_address'].lower()
                if addr not in tracked_wallets:
                    tracked_wallets[addr] = []
                tracked_wallets[addr].append(row)

            # Max blocks to fetch at once to prevent RPC timeouts
            end_block = min(start_block + 500, latest_block)
            
            # logger.debug(f"Scanning blocks {start_block} to {end_block}")
            
            logs = w3.eth.get_logs({
                'address': CTF_CONTRACT,
                'fromBlock': start_block,
                'toBlock': end_block,
                'topics': [TRANSFER_SINGLE_TOPIC]
            })
            
            for log in logs:
                _from = ('0x' + log['topics'][2].hex()[-40:]).lower()
                _to   = ('0x' + log['topics'][3].hex()[-40:]).lower()
                
                matches = []
                if _to in tracked_wallets:
                    for row in tracked_wallets[_to]:
                        matches.append((row, "BUY"))
                if _from in tracked_wallets:
                    for row in tracked_wallets[_from]:
                        matches.append((row, "SELL"))
                        
                if matches:
                    token_id = int.from_bytes(log['data'][:32], 'big')
                    amount = int.from_bytes(log['data'][32:], 'big') / 1_000_000.0
                    if amount <= 0:
                        continue
                        
                    tx_hash = log['transactionHash'].hex()
                    ts = int(datetime.now(timezone.utc).timestamp())
                    
                    # Fetch coin info from cache or via API fallback if instantly needed
                    market_info = await fetch_market_info(str(token_id), _from if "SELL" in [m[1] for m in matches] else _to)
                    usd_val = amount * market_info['price']
                    
                    for row, side in matches:
                        # Filters checks
                        if row['only_buys'] and side != "BUY":
                            continue
                        if usd_val < row['min_usd_threshold']:
                            continue
                            
                        # Build Alert
                        poly_url = f"https://polymarket.com/profile/{row['wallet_address']}?tab=activity"
                        
                        msg = format_trade_alert(
                            trade_type     = side,
                            size           = amount,
                            price          = market_info['price'],
                            usd_value      = usd_val,
                            outcome        = market_info['outcome'],
                            market_title   = market_info['title'],
                            wallet_address = row['wallet_address'],
                            nickname       = row['nickname'],
                            timestamp      = ts,
                            polymarket_url = poly_url,
                        )
                        
                        logger.info(f"⚡ FAST ALERT 🔥: {side} {amount} shares on {market_info['title'][:30]}... for user {row['chat_id']}")
                        
                        try:
                            # Send message asynchronously via telegram bot object
                            await app.bot.send_message(
                                chat_id=row['chat_id'],
                                text=msg,
                                parse_mode='MarkdownV2',
                                disable_web_page_preview=True
                            )
                        except Exception as e:
                            logger.error(f"Failed to send fast alert: {e}")
                            
            # Update cursor and wait for next Polygon block (~2.5s)
            start_block = end_block + 1
            await asyncio.sleep(2.5)

        except Web3Exception as we:
            logger.warning(f"Web3 RPC Error: {we}")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Scan loop error: {e}", exc_info=True)
            await asyncio.sleep(5)
