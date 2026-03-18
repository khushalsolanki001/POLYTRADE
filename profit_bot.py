import asyncio
import logging
import json
import csv
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import aiohttp
import websockets
from db import get_all_wallets, get_paper_balance, update_paper_balance, upsert_paper_position, init_db
from api import get_crypto_price_markets, get_market_tokens, fetch_trades, parse_trade_timestamp, parse_trade_outcome, parse_trade_price, parse_trade_usd_value

# ─── Configuration ───────────────────────────────────────────────────────────
LOG_FILE = "trades.csv"
BANKROLL_START = 10000.0  # USDC
FRACTIONAL_KELLY = 0.30
MAX_POS_SIZE_PCT = 0.05
MAX_DAILY_LOSS_PCT = 0.08
MIN_EDGE = 0.055
MAX_SPREAD = 0.06
MIN_DEPTH = 2000

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
POLY_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PolyProfitBot")

class PolyProfitBot:
    def __init__(self):
        self.bankroll = BANKROLL_START
        self.prices = {"BTC": [], "ETH": [], "SOL": []}  # (timestamp, price)
        self.clob_data = {}  # token_id -> {best_bid, best_ask, depth, mid, spread}
        self.active_markets = []
        self.open_positions = []
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.last_run_time = 0
        self.market_tokens = {} # condition_id -> {"YES": id, "NO": id}
        self.subscribed_tokens = set()
        
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "market_id", "direction", "size_usd", "edge", "outcome", "pnl"])

    async def run(self):
        logger.info(f"🚀 PolyProfitBot starting with ${self.bankroll} USDC")
        init_db()
        
        # Start background tasks
        asyncio.create_task(self.binance_ws_loop())
        asyncio.create_task(self.polymarket_ws_loop())
        asyncio.create_task(self.market_sync_loop())
        
        while True:
            try:
                await self.trading_loop()
                await asyncio.sleep(20)
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def binance_ws_loop(self):
        """Listen to Binance for real-time BTC/ETH/SOL prices."""
        streams = "btcusdt@ticker/ethusdt@ticker/solusdt@ticker"
        async with websockets.connect(f"{BINANCE_WS_URL}/{streams}") as ws:
            while True:
                try:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    symbol = data['s'].replace('USDT', '')
                    price = float(data['c'])
                    now = time.time()
                    self.prices[symbol].append((now, price))
                    
                    # Keep only last 6 minutes of data (need 3min ago)
                    self.prices[symbol] = [(t, p) for t, p in self.prices[symbol] if now - t < 360]
                except Exception as e:
                    logger.warning(f"Binance WS error: {e}")
                    await asyncio.sleep(5)

    async def polymarket_ws_loop(self):
        """Listen to Polymarket CLOB for real-time order book data."""
        while True:
            try:
                async with websockets.connect(POLY_CLOB_WS_URL) as ws:
                    logger.info("Connected to Polymarket CLOB WS")
                    while True:
                        # Subscribe to new tokens if any
                        tokens_to_sub = []
                        for m_id, tokens in self.market_tokens.items():
                            for t_id in tokens.values():
                                if t_id and t_id not in self.subscribed_tokens:
                                    tokens_to_sub.append(t_id)
                                    self.subscribed_tokens.add(t_id)
                        
                        if tokens_to_sub:
                            sub_msg = {
                                "type": "subscribe",
                                "assets": tokens_to_sub,
                                "channels": ["book", "best_bid_ask"]
                            }
                            await ws.send(json.dumps(sub_msg))
                            logger.info(f"📡 Subscribed to {len(tokens_to_sub)} tokens on CLOB: {tokens_to_sub}")

                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            data = json.loads(msg)
                        except asyncio.TimeoutError:
                            continue
                        
                        if data.get("type") == "best_bid_ask":
                            t_id = data.get("asset_id")
                            bid = float(data.get("best_bid", 0))
                            ask = float(data.get("best_ask", 0))
                            if bid > 0 and ask > 0:
                                mid = (bid + ask) / 2
                                spread = ask - bid
                                self.clob_data[t_id] = self.clob_data.get(t_id, {})
                                self.clob_data[t_id].update({
                                    "best_bid": bid,
                                    "best_ask": ask,
                                    "mid": mid,
                                    "spread": spread
                                })
                        
                        elif data.get("type") == "book":
                            t_id = data.get("asset_id")
                            bids = data.get("bids", [])
                            asks = data.get("asks", [])
                            d = self.clob_data.get(t_id, {})
                            mid = d.get("mid")
                            if mid:
                                depth = 0
                                for b in bids:
                                    if float(b['price']) >= mid - 0.02: depth += float(b['size'])
                                for a in asks:
                                    if float(a['price']) <= mid + 0.02: depth += float(a['size'])
                                self.clob_data[t_id]["depth"] = depth

            except Exception as e:
                logger.warning(f"Polymarket WS error: {e}")
                await asyncio.sleep(5)

    async def market_sync_loop(self):
        """Periodically sync active markets and tokens."""
        while True:
            try:
                markets = await get_crypto_price_markets()
                logger.info(f"🔍 Found {len(markets)} active crypto 5/15-min markets")
                self.active_markets = markets
                for m in markets:
                    c_id = m['conditionId']
                    if c_id not in self.market_tokens:
                        tokens = await get_market_tokens(c_id)
                        self.market_tokens[c_id] = tokens
                        logger.info(f"📍 Tracked tokens for market: {m.get('question')} | {tokens}")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Market sync error: {e}")
                await asyncio.sleep(30)

    async def get_smart_copy_score(self) -> float:
        """Calculate smart copy score based on watched wallets."""
        wallets = get_all_wallets()
        if not wallets: return 0.0
        
        # Filter "smart short-term whales" (placeholder win rate check)
        # In a real bot, we'd analyze their 5/15-min market performance
        # For now, if we see 3+ whales buying the same side in last 30s, score +0.05
        # This requires historical trade data we collect in scanner.py
        return 0.0 # Placeholder

    def calculate_momentum_p(self, symbol: str) -> float:
        """Calculate momentum probability: (P_now - P_1min_ago)/P_1min_ago * 12 + (P_now - P_3min_ago)/P_3min_ago * 8"""
        prices = self.prices.get(symbol, [])
        if len(prices) < 20: return 0.5 # Need some history
        
        now_t, now_p = prices[-1]
        
        def get_price_ago(seconds):
            target = now_t - seconds
            # Linear search back
            for t, p in reversed(prices):
                if t <= target: return p
            return prices[0][1]

        p_1min = get_price_ago(60)
        p_3min = get_price_ago(180)
        
        mom_1 = (now_p - p_1min) / p_1min
        mom_3 = (now_p - p_3min) / p_3min
        
        momentum_factor = (mom_1 * 12) + (mom_3 * 8)
        return max(0.05, min(0.95, 0.5 + momentum_factor))

    async def trading_loop(self):
        """Core execution loop every 10-30s."""
        for m in self.active_markets:
            c_id = m['conditionId']
            tokens = self.market_tokens.get(c_id)
            if not tokens: continue
            
            # Get mid, spread, depth for YES token
            yes_id = tokens.get("YES")
            if not yes_id or yes_id not in self.clob_data: continue
            
            data = self.clob_data[yes_id]
            mid = data.get("mid")
            spread = data.get("spread")
            depth = data.get("depth", 0)
            
            if not mid or not spread: continue
            
            # Momentum
            symbol = "BTC" if "Bitcoin" in m['question'] else "ETH" if "Ethereum" in m['question'] else "SOL"
            your_p = self.calculate_momentum_p(symbol)
            
            # Smart Score
            smart_score = await self.get_smart_copy_score()
            
            # Final Probability
            final_p = (your_p * 0.85) + (smart_score * 0.15)
            
            # Edge Check
            edge = final_p - mid
            
            # Rule 6: Skip conditions
            if abs(edge) < MIN_EDGE or spread > MAX_SPREAD or depth < MIN_DEPTH:
                continue
                
            # Rule 7: Extra safety < 60s left
            # (Need to check market end time)
            
            # Rule 7: Sizing (Fractional Kelly)
            direction = "YES" if edge > 0 else "NO"
            # If direction is NO, we use mid of NO token (1 - mid of YES)
            actual_mid = mid if direction == "YES" else (1 - mid)
            actual_edge = abs(edge)
            
            f = (actual_edge / (1 - actual_mid)) * FRACTIONAL_KELLY
            pos_usd = self.bankroll * min(f, MAX_POS_SIZE_PCT)
            
            # Rule 9: Risk rules
            # (Placeholder for daily loss check and simultaneous positions)
            
            # 8. Execution
            self.execute_paper_trade(m, direction, pos_usd, mid, edge)

    def execute_paper_trade(self, market, direction, size, mid, edge):
        # Rule 8: Simulate fill at mid + 0.005 slippage
        fill_price = mid + 0.005 if direction == "YES" else mid - 0.005
        logger.info(f"✅ PAPER BUY {direction} ${size:.0f} at mid {mid:.4f} | edge {edge*100:.1f}% | fill ≈ {fill_price:.4f}")
        
        # Update bankroll (paper)
        self.bankroll -= size
        
        # Log to CSV
        with open(LOG_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now().isoformat(), market['conditionId'], direction, size, edge, "OPEN", 0])

if __name__ == "__main__":
    bot = PolyProfitBot()
    asyncio.run(bot.run())

if __name__ == "__main__":
    bot = PolyProfitBot()
    asyncio.run(bot.run())
