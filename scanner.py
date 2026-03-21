import asyncio
import logging
from web3 import Web3
from web3.exceptions import Web3Exception
import aiohttp
from datetime import datetime, timezone
import json

from db import get_all_wallets, update_last_timestamp, get_setting, set_setting
from handlers import format_trade_alert
from api import (
    fetch_trades, get_trade_title,
    parse_trade_outcome, parse_trade_price,
    parse_trade_type, parse_trade_size,
    parse_trade_usd_value, parse_trade_timestamp,
)

logger = logging.getLogger(__name__)

RPC_URL = "https://polygon.drpc.org"
def get_w3():
    return Web3(Web3.HTTPProvider(RPC_URL))

CTF_CONTRACT = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

_asset_cache = {}


async def _fetch_api_trade(wallet: str, token_id_str: str) -> dict | None:
    """
    After an on-chain event fires, fetch the matching trade from the Polymarket
    Data API to get accurate title, outcome, price and USDC size.

    Strategy:
      1. Try to match by asset token-ID (exact).
      2. Fall back to the most-recent trade if it landed within the last 60 s.
    Retries once after a 3 s pause to absorb Polymarket's indexing delay.
    """
    for attempt in range(2):
        if attempt > 0:
            await asyncio.sleep(3)
        try:
            trades = await fetch_trades(wallet, limit=10)
            for t in trades:
                if str(t.get("asset", "")) == token_id_str:
                    return t
            # Fallback: newest trade that arrived within 60 s
            if trades:
                now_ts   = int(datetime.now(timezone.utc).timestamp())
                trade_ts = parse_trade_timestamp(trades[0])
                if abs(trade_ts - now_ts) <= 60:
                    return trades[0]
        except Exception as exc:
            logger.warning("API trade lookup attempt %d failed for %s: %s",
                           attempt + 1, wallet[:10], exc)
    return None


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
    # Brief startup pause so the bot is fully ready before we start paginating
    await asyncio.sleep(5)
    while True:
        try:
            url = "https://clob.polymarket.com/markets?active=true"
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while url:
                    async with session.get(url) as resp:
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
                        # Yield control between pages to avoid starving other tasks
                        await asyncio.sleep(0)

            logger.info("Token cacher: cache populated with %d assets. Sleeping 120s.", len(_asset_cache))
            # Sleep before re-syncing to avoid slamming CLOB
            await asyncio.sleep(120)
        except asyncio.CancelledError:
            logger.info("Token cacher: cancelled cleanly.")
            return
        except Exception as e:
            logger.error("Token cacher error: %s", e)
            await asyncio.sleep(15)


_cacher_task: asyncio.Task | None = None

async def run_block_scanner(app):
    """Background task to scan blocks every 3 seconds for instant alerts."""
    global _cacher_task

    # Start the token cacher as a tracked task so we can cancel it cleanly
    _cacher_task = asyncio.create_task(run_market_cacher())
    
    w3 = get_w3()
    
    # Try to load last block from DB
    last_saved_block = get_setting("last_scanned_block")
    if last_saved_block:
        start_block = int(last_saved_block)
        logger.info(f"🚀 Block scanner resuming from DB block {start_block}")
    else:
        # We start searching from 5 blocks behind current to catch up
        try:
            start_block = w3.eth.block_number - 5
            logger.info(f"🚀 Block scanner started from block {start_block} (default -5)")
        except Exception as e:
            logger.error(f"Failed to get initial block number: {e}")
            start_block = 0
    
    try:
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
                        raw_amount = int.from_bytes(log['data'][32:], 'big') / 1_000_000.0
                        if raw_amount <= 0:
                            continue

                        tx_hash = log['transactionHash'].hex()
                        ts = int(datetime.now(timezone.utc).timestamp())

                        # Use the tracked wallet address for the API call.
                        # BUY  → tracked wallet is in _to  (receiving shares)
                        # SELL → tracked wallet is in _from (sending shares)
                        primary_wallet = matches[0][0]["wallet_address"]

                        # ── Fetch accurate trade data from Polymarket Data API ──
                        # This gives us correct title, outcome, price and USDC size
                        # instead of relying on the incomplete on-chain token cache.
                        api_trade = await _fetch_api_trade(primary_wallet, str(token_id))

                        if api_trade:
                            api_type    = parse_trade_type(api_trade)
                            api_size    = parse_trade_size(api_trade)
                            api_price   = parse_trade_price(api_trade)
                            api_usd     = parse_trade_usd_value(api_trade)
                            api_outcome = parse_trade_outcome(api_trade)
                            api_title   = get_trade_title(api_trade) or "Unknown Market"
                            api_ts      = parse_trade_timestamp(api_trade) or ts
                            logger.info(
                                "⚡ API trade matched: %s %s %.2f USDC on %s",
                                api_type, api_outcome, api_usd, api_title[:40],
                            )
                        else:
                            # Fallback to raw on-chain data (no title/outcome available)
                            market_info = await fetch_market_info(str(token_id), primary_wallet)
                            api_type    = None
                            api_size    = raw_amount
                            api_price   = market_info["price"]
                            api_usd     = raw_amount * api_price
                            api_outcome = market_info["outcome"]
                            api_title   = market_info["title"]
                            api_ts      = ts
                            logger.warning(
                                "⚠️ API trade not found for token %s wallet %s — using raw on-chain data",
                                token_id, primary_wallet[:10],
                            )

                        for row, side in matches:
                            trade_type = api_type or side  # API type takes priority
                            # Filters checks
                            if row['only_buys'] and trade_type != "BUY":
                                continue
                            if api_usd < row['min_usd_threshold']:
                                continue

                            # Build Alert
                            poly_url = f"https://polymarket.com/profile/{row['wallet_address']}?tab=activity"

                            msg = format_trade_alert(
                                trade_type     = trade_type,
                                size           = api_size,
                                price          = api_price,
                                usd_value      = api_usd,
                                outcome        = api_outcome,
                                market_title   = api_title,
                                wallet_address = row['wallet_address'],
                                nickname       = row['nickname'],
                                timestamp      = api_ts,
                                polymarket_url = poly_url,
                            )

                            logger.info(
                                "⚡ FAST ALERT 🔥: %s %s $%.2f on %s for chat %s",
                                trade_type, api_outcome, api_usd, api_title[:30], row['chat_id'],
                            )

                            try:
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
                set_setting("last_scanned_block", str(start_block))
                await asyncio.sleep(2.5)

            except Web3Exception as we:
                logger.warning(f"Web3 RPC Error: {we}")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Scan loop error: {e}", exc_info=True)
                await asyncio.sleep(5)
    finally:
        if _cacher_task:
            _cacher_task.cancel()
            try:
                await _cacher_task
            except asyncio.CancelledError:
                pass
        logger.info("Block scanner: shut down and cleaned up.")

