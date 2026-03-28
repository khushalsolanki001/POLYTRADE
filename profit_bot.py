import asyncio
import logging
import json
import csv
import os
import time
from datetime import datetime, timezone

import websockets

from db import init_db
from api import get_crypto_price_markets, get_market_tokens

# ─── Configuration ───────────────────────────────────────────────────────────
LOG_FILE = "trades.csv"
BANKROLL_START = 10000.0  # USDC
FRACTIONAL_KELLY = 0.30
MAX_POS_SIZE_PCT = 0.05
MAX_DAILY_LOSS_PCT = 0.08
MAX_CONSECUTIVE_LOSSES = 4
PAUSE_DURATION = 600  # 10 minutes in seconds
MAX_SIMULTANEOUS_POSITIONS = 3
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
        self.daily_start_bankroll = BANKROLL_START
        self.last_day = datetime.now(timezone.utc).date()

        self.prices = {"BTC": [], "ETH": [], "SOL": []}  # (timestamp, price)
        self.clob_data = {}  # token_id -> {best_bid, best_ask, depth, mid, spread}
        self.active_markets = []
        self.market_tokens = {} # condition_id -> {"YES": id, "NO": id}
        self.subscribed_tokens = set()

        # Risk & Tracking
        self.open_positions = {}  # condition_id -> {direction, size, fill_price}
        self.traded_markets = set() # Track to enforce 1 trade per market interval
        self.consecutive_losses = 0
        self.pause_until = 0

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
                await asyncio.sleep(10)  # Core execution loop every 10-30s
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def binance_ws_loop(self):
        """Listen to Binance for real-time BTC/ETH/SOL prices."""
        streams = "btcusdt@ticker/ethusdt@ticker/solusdt@ticker"
        while True:
            try:
                async with websockets.connect(f"{BINANCE_WS_URL}/{streams}") as ws:
                    logger.info("📡 Connected to Binance WS")
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        symbol = data['s'].replace('USDT', '')
                        price = float(data['c'])
                        now = time.time()
                        self.prices[symbol].append((now, price))

                        # Keep only last 4 minutes of data (need 3min ago)
                        self.prices[symbol] = [(t, p) for t, p in self.prices[symbol] if now - t < 240]
            except Exception as e:
                logger.warning(f"Binance WS error: {e}")
                await asyncio.sleep(5)

    async def polymarket_ws_loop(self):
        """Listen to Polymarket CLOB for real-time order book data."""
        while True:
            try:
                async with websockets.connect(POLY_CLOB_WS_URL) as ws:
                    logger.info("📡 Connected to Polymarket CLOB WS")
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
                                "channels": ["book", "best_bid_ask", "price_change", "last_trade_price"]
                            }
                            await ws.send(json.dumps(sub_msg))
                            logger.info(f"📡 Subscribed to {len(tokens_to_sub)} new tokens on CLOB")

                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
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
                                    if float(b['price']) >= mid - 0.02:
                                        depth += float(b['size'])
                                for a in asks:
                                    if float(a['price']) <= mid + 0.02:
                                        depth += float(a['size'])
                                self.clob_data[t_id]["depth"] = depth

            except Exception as e:
                logger.warning(f"Polymarket WS error: {e}")
                await asyncio.sleep(5)

    async def market_sync_loop(self):
        """Periodically sync active markets and check for resolutions."""
        while True:
            try:
                markets = await get_crypto_price_markets()
                self.active_markets = markets

                # Check for resolved markets
                active_c_ids = {m['conditionId'] for m in markets}
                resolved_c_ids = [c_id for c_id in self.open_positions if c_id not in active_c_ids]

                for c_id in resolved_c_ids:
                    # If market is no longer active, simulate resolution logic (pseudo-resolution for paper)
                    # In a real bot, we'd query Gamma API for actual resolution outcome
                    await self.resolve_paper_trade(c_id, outcome="YES") # Stubbing outcome for now

                for m in markets:
                    c_id = m['conditionId']
                    if c_id not in self.market_tokens:
                        tokens = await get_market_tokens(c_id)
                        if tokens and tokens.get("YES"):
                            self.market_tokens[c_id] = tokens

                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Market sync error: {e}")
                await asyncio.sleep(30)

    async def get_smart_copy_score(self) -> float:
        """Calculate smart copy score based on watched wallets (+0.05 if >=3 whales buying)."""
        # Placeholder for DB integration. Returns dummy score.
        return 0.0

    def calculate_momentum_p(self, symbol: str) -> float:
        """Calculate momentum probability: (P_now - P_1min_ago)/P_1min_ago * 12 + (P_now - P_3min_ago)/P_3min_ago * 8"""
        prices = self.prices.get(symbol, [])
        if len(prices) < 20:
            return 0.5  # Need some history

        now_t, now_p = prices[-1]

        def get_price_ago(seconds):
            target = now_t - seconds
            for t, p in reversed(prices):
                if t <= target:
                    return p
            return prices[0][1]

        p_1min = get_price_ago(60)
        p_3min = get_price_ago(180)

        mom_1 = (now_p - p_1min) / p_1min
        mom_3 = (now_p - p_3min) / p_3min

        momentum_factor = (mom_1 * 12) + (mom_3 * 8)

        return max(0.05, min(0.95, 0.5 + momentum_factor))

    async def trading_loop(self):
        """Core execution loop every 10-30s."""
        # 9. Risk Rules: Pause check
        if time.time() < self.pause_until:
            return

        # 9. Risk Rules: Daily loss check
        today = datetime.now(timezone.utc).date()
        if today > self.last_day:
            self.daily_start_bankroll = self.bankroll
            self.last_day = today
            self.consecutive_losses = 0

        daily_pnl_pct = (self.bankroll - self.daily_start_bankroll) / self.daily_start_bankroll
        if daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
            logger.warning(f"🛑 Daily loss limit reached ({daily_pnl_pct*100:.1f}%). Trading stopped for today.")
            return

        for m in self.active_markets:
            c_id = m['conditionId']

            # 9. Risk Rules: Max 1 trade per market interval
            if c_id in self.traded_markets:
                continue

            # 9. Risk Rules: Max 3 simultaneous positions
            if len(self.open_positions) >= MAX_SIMULTANEOUS_POSITIONS:
                break

            tokens = self.market_tokens.get(c_id)
            if not tokens:
                continue

            yes_id = tokens.get("YES")
            no_id = tokens.get("NO")
            if not yes_id or yes_id not in self.clob_data:
                continue

            yes_data = self.clob_data[yes_id]
            mid = yes_data.get("mid")
            spread = yes_data.get("spread")
            depth = yes_data.get("depth", 0)

            if not mid or not spread:
                continue

            # 10. Special Edge: Arbitrage check
            if no_id in self.clob_data:
                no_data = self.clob_data[no_id]
                yes_ask = yes_data.get("best_ask", 1.0)
                no_ask = no_data.get("best_ask", 1.0)
                if yes_ask + no_ask < 0.99:
                    logger.info(f"🚨 ARBITRAGE OPPORTUNITY: YES ask {yes_ask} + NO ask {no_ask} = {yes_ask+no_ask}")

            # 1. & 2. Momentum
            symbol = "BTC" if "Bitcoin" in m['question'] else "ETH" if "Ethereum" in m['question'] else "SOL"
            your_p = self.calculate_momentum_p(symbol)

            # 4. Smart Score
            smart_score = await self.get_smart_copy_score()

            # 5. Final Probability
            final_p = (your_p * 0.85) + (smart_score * 0.15)

            # 6. Edge Check
            edge = final_p - mid

            # 7. Sizing & Safety
            end_date_str = m.get('endDate')
            seconds_left = 9999
            if end_date_str:
                try:
                    end_time = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    seconds_left = (end_time - datetime.now(timezone.utc)).total_seconds()
                except ValueError:
                    pass

            required_edge = MIN_EDGE
            if seconds_left < 60:
                required_edge = 0.08  # 10. Special Edge: Last 60 seconds

            if abs(edge) < required_edge or spread > MAX_SPREAD or depth < MIN_DEPTH:
                continue

            # 7. Position Sizing (Fractional Kelly)
            direction = "YES" if edge > 0 else "NO"
            actual_mid = mid if direction == "YES" else (1 - mid)
            actual_edge = abs(edge)

            f = (actual_edge / (1 - actual_mid)) * FRACTIONAL_KELLY
            pos_usd = self.bankroll * min(f, MAX_POS_SIZE_PCT)

            # 8. Execution
            self.execute_paper_trade(c_id, direction, pos_usd, actual_mid, actual_edge)

    def execute_paper_trade(self, condition_id, direction, size, mid, edge):
        # 8. Simulate fill at mid + 0.005 slippage
        fill_price = mid + 0.005
        if fill_price >= 0.99:
            return

        logger.info(f"PAPER BUY {direction} ${size:.0f} at mid {mid:.4f} | edge {edge*100:.1f}% | estimated fill ≈ {fill_price:.4f} slippage")

        self.bankroll -= size
        self.traded_markets.add(condition_id)
        self.open_positions[condition_id] = {
            "direction": direction,
            "size_usd": size,
            "fill_price": fill_price,
            "shares": size / fill_price
        }
        logger.info(f"💰 LIVE BANKROLL: ${self.bankroll:.2f} USDC")

    async def resolve_paper_trade(self, condition_id, outcome="YES"):
        """Simulates market resolution."""
        if condition_id not in self.open_positions:
            return

        pos = self.open_positions.pop(condition_id)
        win = (pos["direction"] == outcome)

        if win:
            pnl = pos["shares"] * 1.0 - pos["size_usd"]
            self.bankroll += pos["shares"] * 1.0
            self.consecutive_losses = 0
            logger.info(f"🏆 WON TRADE. Profit: +${pnl:.2f}")
        else:
            pnl = -pos["size_usd"]
            self.consecutive_losses += 1
            logger.info(f"💀 LOST TRADE. Loss: ${abs(pnl):.2f}")

            # 9. Risk Rules: Pause after 4 losses
            if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                logger.warning("🛑 4 losses in a row! Pausing trading for 10 minutes.")
                self.pause_until = time.time() + PAUSE_DURATION
                self.consecutive_losses = 0

        logger.info(f"💰 LIVE BANKROLL: ${self.bankroll:.2f} USDC")

        # 9. Log to CSV
        with open(LOG_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now(timezone.utc).isoformat(), condition_id, pos["direction"], pos["size_usd"], 0, outcome, pnl])

        self.send_telegram_update(f"Trade resolved. PnL: ${pnl:.2f}. Bankroll: ${self.bankroll:.2f}")

    def send_telegram_update(self, msg: str):
        """Send Telegram update only on big moves or daily summary."""
        logger.info(f"📱 [TELEGRAM] {msg}")

if __name__ == "__main__":
    bot = PolyProfitBot()
    asyncio.run(bot.run())
