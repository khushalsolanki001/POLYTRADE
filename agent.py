"""
agent.py — Autonomous BTC 5-Minute Paper Trading Agent
=======================================================

Runs as a background asyncio task inside bot.py.
Every AGENT_POLL_SECONDS seconds it:
  1. Reads real-time BTC price from a Binance WebSocket
  2. Computes 1-min + 3-min momentum probability
  3. If edge > AGENT_MIN_EDGE → places a paper Up or Down trade
  4. Detects market resolution (slug change) → auto-sells & reports P&L
  5. Sends Telegram notifications for every BUY / SELL / error

Paper trades use the same SQLite DB as regular user trades.
The agent's dedicated account uses sentinel user ID: AGENT_USER_ID
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import websockets
from dotenv import load_dotenv

import db
from handlers import (
    _paper_buy_core,
    _paper_sell_core,
    _get_target_market,
    _format_5m_et_label,
    _esc,
    _esc_code,
)

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
AGENT_USER_ID: int   = int(os.getenv("AGENT_USER_ID", "999999"))  # sentinel paper-trade user
AGENT_CHAT_ID: int   = int(os.getenv("AGENT_CHAT_ID", "0"))       # Telegram chat for alerts
AGENT_TRADE_USD: float  = float(os.getenv("AGENT_TRADE_USD", "5"))
AGENT_MIN_EDGE: float   = float(os.getenv("AGENT_MIN_EDGE", "0.06"))
AGENT_POLL_SECONDS: int = int(os.getenv("AGENT_POLL_SECONDS", "60"))

MAX_CONSECUTIVE_LOSSES = 4
PAUSE_DURATION         = 600   # 10-minute cool-down after 4 losses
MAX_DAILY_LOSS_PCT     = 0.15  # stop for the day if down 15%
STARTING_BALANCE       = 100.0
MIN_PRICE_SAMPLES      = 30    # need at least 30 BTC price ticks for signal

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
USER_TZ        = ZoneInfo("Asia/Kolkata")
MARKET_TZ      = ZoneInfo("America/New_York")

logger = logging.getLogger("PolyAgent")


# ─── Module-level mutable state ───────────────────────────────────────────────
class _AgentState:
    enabled: bool = True
    running: bool = False
    prices: list   = []           # list of (unix_timestamp, btc_price)
    current_slug: Optional[str]    = None  # slug of the open position's market
    current_outcome: Optional[str] = None  # "Up" or "Down"
    current_buy_price: float = 0.0
    current_shares: float   = 0.0
    consecutive_losses: int  = 0
    pause_until: float       = 0.0
    last_action: str         = "None yet"
    session_wins: int        = 0
    session_losses: int      = 0
    daily_start_balance: Optional[float] = None
    last_day: Optional[object] = None


_state = _AgentState()


# ─────────────────────────────────────────────────────────────────────────────
#  Binance Price Feed
# ─────────────────────────────────────────────────────────────────────────────

async def _binance_ws_loop() -> None:
    """Maintain a Binance WebSocket connection for real-time BTC/USDT price."""
    while True:
        try:
            async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                logger.info("[AGENT] Connected to Binance WS ✅")
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    price = float(data["c"])   # 'c' = latest close/last price
                    now   = time.time()
                    _state.prices.append((now, price))
                    # Keep only the last 5 minutes of ticks
                    _state.prices = [(t, p) for t, p in _state.prices if now - t < 300]
        except Exception as exc:
            logger.warning("[AGENT] Binance WS error: %s — reconnecting in 5s", exc)
            await asyncio.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
#  Momentum Calculation
# ─────────────────────────────────────────────────────────────────────────────

def _price_ago(seconds: float) -> Optional[float]:
    """Return BTC price approximately `seconds` ago, or None if unavailable."""
    if not _state.prices:
        return None
    now_t = _state.prices[-1][0]
    target = now_t - seconds
    for t, p in reversed(_state.prices):
        if t <= target:
            return p
    return _state.prices[0][1]  # oldest available


def _calculate_momentum() -> Optional[float]:
    """
    Compute a momentum-based probability (0.05–0.95) that BTC will be UP
    at the end of the 5-minute window.

    Returns None when there is insufficient price history.
    Formula: 0.5 + (mom_1min × 12) + (mom_3min × 8), clamped to [0.05, 0.95]
    """
    if len(_state.prices) < MIN_PRICE_SAMPLES:
        return None

    now_p = _state.prices[-1][1]
    p1 = _price_ago(60)
    p3 = _price_ago(180)

    if p1 is None or p3 is None:
        return None

    mom1 = (now_p - p1) / p1
    mom3 = (now_p - p3) / p3
    factor = mom1 * 12 + mom3 * 8
    return max(0.05, min(0.95, 0.5 + factor))


# ─────────────────────────────────────────────────────────────────────────────
#  Telegram Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _send(bot, text: str) -> None:
    """Send a MarkdownV2 Telegram message to AGENT_CHAT_ID (silently ignores errors)."""
    if not AGENT_CHAT_ID:
        return
    try:
        await bot.send_message(
            chat_id=AGENT_CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        # Fallback: try plain text
        try:
            plain = text.replace("*", "").replace("`", "").replace("\\", "")
            await bot.send_message(chat_id=AGENT_CHAT_ID, text=plain)
        except Exception as exc2:
            logger.warning("[AGENT] Telegram send failed: %s / %s", exc, exc2)


def _et_label_for_slug(slug: str) -> str:
    """Convert a slug like 'btc-updown-5m-1773438900' to an ET time label."""
    try:
        return _format_5m_et_label(int(slug.split("-")[-1]))
    except Exception:
        return slug


def _now_ist() -> str:
    return datetime.now(timezone.utc).astimezone(USER_TZ).strftime("%I:%M:%S %p IST").lstrip("0")


def _fmt_buy_alert(outcome: str, fill_price: float, edge: float,
                   momentum_p: float, slug: str) -> str:
    direction_emoji = "🟢" if outcome.lower() == "up" else "🔴"
    balance = db.get_paper_balance(AGENT_USER_ID)
    return (
        "🤖 *AUTO\\-TRADE: BUY*\n\n"
        f"📊 *Market:* BTC {_esc(_et_label_for_slug(slug))}\n"
        f"🎯 *Direction:* {direction_emoji} *{_esc(outcome.upper())}*\n"
        f"💰 *Size:* `${_esc_code(f'{AGENT_TRADE_USD:.2f}')}`   "
        f"\\|   *Ask:* `${_esc_code(f'{fill_price:.4f}')}`\n"
        f"⚡ *Edge:* `+{_esc_code(f'{edge*100:.1f}')}%`  "
        f"\\(momentum p\\={_esc_code(f'{momentum_p*100:.1f}%')}\\)\n"
        f"💵 *Agent Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"⏰ `{_esc_code(_now_ist())}`"
    )


def _fmt_sell_alert(outcome: str, sell_price: float, buy_price: float,
                    shares: float, pnl: float, slug: str) -> str:
    win = pnl >= 0
    result_emoji = "✅ WIN" if win else "❌ LOSS"
    pnl_sign = "+" if win else "\\-"
    balance = db.get_paper_balance(AGENT_USER_ID)
    return (
        "🤖 *AUTO\\-TRADE: SELL*\n\n"
        f"📊 *Market:* BTC {_esc(_et_label_for_slug(slug))}\n"
        f"🎯 *Sold:* *{_esc(outcome.upper())}*  →  "
        f"`${_esc_code(f'{buy_price:.4f}')}` → `${_esc_code(f'{sell_price:.4f}')}`\n"
        f"📦 *Shares:* `{_esc_code(f'{shares:.2f}')}`\n"
        f"💰 *P&L:* `{pnl_sign}${_esc_code(f'{abs(pnl):.2f}')}` {result_emoji}\n"
        f"💵 *Agent Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"🏆 *Session:* `{_state.session_wins}` wins / `{_state.session_losses}` losses\n"
        f"⏰ `{_esc_code(_now_ist())}`"
    )


def _fmt_pause_alert(reason: str) -> str:
    return f"🛑 *AGENT PAUSED*\n\n_{_esc(reason)}_"


# ─────────────────────────────────────────────────────────────────────────────
#  Auto-Sell on Market Resolution
# ─────────────────────────────────────────────────────────────────────────────

async def _auto_sell(bot) -> None:
    """Called when the active market has changed (previous market resolved)."""
    if not _state.current_slug or not _state.current_outcome:
        return

    old_slug    = _state.current_slug
    old_outcome = _state.current_outcome
    buy_price   = _state.current_buy_price
    shares      = _state.current_shares

    logger.info("[AGENT] Auto-selling %s in %s", old_outcome, old_slug)

    # Clear state BEFORE the async call to avoid double-sell races
    _state.current_slug    = None
    _state.current_outcome = None

    ok, msg = await _paper_sell_core(AGENT_USER_ID, old_outcome, None, old_slug)

    if ok:
        logger.info("[AGENT] Auto-sell OK")

        # Parse sell price from the response message → look for "@ $0.xxxx"
        sell_price = buy_price  # safe fallback
        m = re.search(r"@\s*\$([0-9.]+)", msg.replace("\\", ""))
        if m:
            try:
                sell_price = float(m.group(1))
            except ValueError:
                pass

        pnl = (sell_price - buy_price) * shares
        win = pnl >= 0

        if win:
            _state.session_wins += 1
            _state.consecutive_losses = 0
        else:
            _state.session_losses += 1
            _state.consecutive_losses += 1
            if _state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                _state.pause_until = time.time() + PAUSE_DURATION
                _state.consecutive_losses = 0
                await _send(bot, _fmt_pause_alert(
                    f"4 consecutive losses. Pausing for {PAUSE_DURATION // 60} minutes."
                ))

        _state.last_action = f"SELL {old_outcome.upper()} (pnl {'+' if win else ''}{pnl:.2f})"
        await _send(bot, _fmt_sell_alert(
            outcome=old_outcome,
            sell_price=sell_price,
            buy_price=buy_price,
            shares=shares,
            pnl=pnl,
            slug=old_slug,
        ))
    else:
        logger.warning("[AGENT] Auto-sell failed: %s", msg)
        clean_msg = str(msg).replace("\\", "")[:200]
        _state.last_action = f"SELL FAILED"
        await _send(bot, f"⚠️ *Agent sell failed*\n_{_esc(clean_msg)}_")


# ─────────────────────────────────────────────────────────────────────────────
#  Core Trading Cycle
# ─────────────────────────────────────────────────────────────────────────────

async def _trading_cycle(bot) -> None:
    """Single trading decision cycle."""
    if not _state.enabled:
        return

    # Pause check
    if time.time() < _state.pause_until:
        remaining = int(_state.pause_until - time.time())
        logger.info("[AGENT] On cool-down — %ds remaining", remaining)
        return

    # Daily loss limit check
    today   = datetime.now(timezone.utc).date()
    balance = db.get_paper_balance(AGENT_USER_ID)

    if _state.last_day is None or today != _state.last_day:
        _state.daily_start_balance = balance
        _state.last_day = today

    if _state.daily_start_balance and _state.daily_start_balance > 0:
        daily_loss_pct = (balance - _state.daily_start_balance) / _state.daily_start_balance
        if daily_loss_pct <= -MAX_DAILY_LOSS_PCT:
            logger.warning("[AGENT] Daily loss limit %.1f%% reached. Stopping for today.",
                           daily_loss_pct * 100)
            _state.last_action = f"Paused: daily loss {daily_loss_pct*100:.1f}%"
            return

    # Fetch current active market
    market, mode = await _get_target_market()
    if not market:
        logger.info("[AGENT] No tradeable market found.")
        return

    current_slug = market.get("slug", "")

    # Auto-sell if the market has rolled over to a new window
    if _state.current_slug and current_slug and current_slug != _state.current_slug:
        logger.info("[AGENT] Market changed: %s → %s. Triggering auto-sell.",
                    _state.current_slug, current_slug)
        await _auto_sell(bot)

    # Skip if we're already holding a position (wait for it to resolve)
    if _state.current_slug:
        logger.info("[AGENT] Holding position in %s. Waiting for resolution.", _state.current_slug)
        return

    # Get momentum signal
    momentum_p = _calculate_momentum()
    if momentum_p is None:
        logger.info("[AGENT] Not enough price history (%d ticks). Waiting.", len(_state.prices))
        return

    edge = abs(momentum_p - 0.5)
    if edge < AGENT_MIN_EDGE:
        logger.info("[AGENT] Edge %.3f < %.3f — skipping.", edge, AGENT_MIN_EDGE)
        _state.last_action = f"Skipped — edge {edge*100:.1f}% < {AGENT_MIN_EDGE*100:.1f}%"
        return

    outcome = "Up" if momentum_p > 0.5 else "Down"
    logger.info("[AGENT] Signal: %s | p=%.3f | edge=%.3f", outcome, momentum_p, edge)

    # Ensure enough balance
    if balance < AGENT_TRADE_USD:
        logger.warning("[AGENT] Insufficient balance ($%.2f) for $%.2f trade.", balance, AGENT_TRADE_USD)
        _state.last_action = f"Skipped — low balance ${balance:.2f}"
        return

    # Execute paper buy
    ok, msg, slug = await _paper_buy_core(AGENT_USER_ID, outcome, AGENT_TRADE_USD)
    if ok and slug:
        # Parse fill price from the buy confirmation message
        fill_price = 0.5  # safe fallback
        price_m = re.search(r"Buy price.*?\$([0-9.]+)", str(msg).replace("\\", ""))
        if price_m:
            try:
                fill_price = float(price_m.group(1))
            except ValueError:
                pass

        # Also compute shares from the confirmation message
        shares_m = re.search(r"Shares bought.*?([0-9.]+)", str(msg).replace("\\", ""))
        shares = AGENT_TRADE_USD / fill_price  # fallback
        if shares_m:
            try:
                shares = float(shares_m.group(1))
            except ValueError:
                pass

        _state.current_slug     = slug
        _state.current_outcome  = outcome
        _state.current_buy_price = fill_price
        _state.current_shares   = shares
        _state.last_action      = f"BUY {outcome.upper()} @ {fill_price:.4f} in {slug}"

        logger.info("[AGENT] BUY OK: %s @ %.4f in %s", outcome, fill_price, slug)
        await _send(bot, _fmt_buy_alert(
            outcome=outcome,
            fill_price=fill_price,
            edge=edge,
            momentum_p=momentum_p,
            slug=slug,
        ))
    else:
        clean = str(msg).replace("\\", "")[:120]
        logger.warning("[AGENT] Buy failed: %s", clean)
        _state.last_action = f"BUY FAILED"


async def _agent_loop(bot) -> None:
    """Outer loop wrapper — runs _trading_cycle every AGENT_POLL_SECONDS."""
    _state.running = True
    db.init_paper_user(AGENT_USER_ID, starting_balance=STARTING_BALANCE)
    logger.info(
        "[AGENT] Trading loop started 🚀 | poll=%ds | trade=$%.2f | min_edge=%.1f%%",
        AGENT_POLL_SECONDS, AGENT_TRADE_USD, AGENT_MIN_EDGE * 100,
    )
    while True:
        try:
            await _trading_cycle(bot)
        except Exception as exc:
            logger.error("[AGENT] Unhandled error in trading cycle: %s", exc, exc_info=True)
        await asyncio.sleep(AGENT_POLL_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
#  Public API — called from bot.py / handlers.py
# ─────────────────────────────────────────────────────────────────────────────

async def start(app) -> None:
    """
    Launch the agent. Called from bot.py `_post_init`.
    Starts two asyncio background tasks:
      - Binance price feed WebSocket
      - Trading cycle loop
    """
    if AGENT_CHAT_ID == 0:
        logger.warning(
            "[AGENT] AGENT_CHAT_ID=0 — Telegram alerts are disabled. "
            "Set AGENT_CHAT_ID=<your_chat_id> in .env to receive trade notifications."
        )
    asyncio.create_task(_binance_ws_loop(), name="agent_binance_ws")
    asyncio.create_task(_agent_loop(app.bot), name="agent_trading_loop")


def toggle() -> bool:
    """Toggle the agent on/off. Returns the new state (True = enabled)."""
    _state.enabled = not _state.enabled
    logger.info("[AGENT] Toggled → %s", "ENABLED" if _state.enabled else "DISABLED")
    return _state.enabled


def is_enabled() -> bool:
    return _state.enabled


def get_status_message() -> str:
    """Return a MarkdownV2 status string for use in /agent_status."""
    paused_for = ""
    if time.time() < _state.pause_until:
        remaining = int(_state.pause_until - time.time())
        paused_for = f"\n⏸ *Cool\\-down:* `{remaining}s` remaining"

    balance = db.get_paper_balance(AGENT_USER_ID)
    pnl = balance - STARTING_BALANCE
    pnl_sign = "+" if pnl >= 0 else "\\-"

    position_text = "None"
    if _state.current_slug and _state.current_outcome:
        try:
            slug_ts = int(_state.current_slug.split("-")[-1])
            et_label = _format_5m_et_label(slug_ts)
            position_text = f"{_state.current_outcome.upper()} @ {et_label}"
        except Exception:
            position_text = f"{_state.current_outcome} in {_state.current_slug}"

    status_emoji = "🟢" if _state.enabled else "🔴"
    feed_status  = "✅" if len(_state.prices) >= MIN_PRICE_SAMPLES else f"⏳ \\({len(_state.prices)} ticks\\)"

    return (
        f"🤖 *PolyAgent Status*\n\n"
        f"{status_emoji} *State:* {'ACTIVE' if _state.enabled else 'PAUSED'}{paused_for}\n"
        f"📡 *BTC feed:* {feed_status}\n"
        f"📍 *Open position:* `{_esc_code(position_text)}`\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"📊 *All\\-time PnL:* `{pnl_sign}${_esc_code(f'{abs(pnl):.2f}')}`\n"
        f"🏆 *Session:* `{_state.session_wins}` wins / `{_state.session_losses}` losses\n"
        f"🔁 *Consecutive losses:* `{_state.consecutive_losses}`\n"
        f"📝 *Last action:* {_esc(_state.last_action)}\n"
        f"⚙️ *Trade size:* `${_esc_code(f'{AGENT_TRADE_USD:.2f}')}`  "
        f"\\|  *Min edge:* `{_esc_code(f'{AGENT_MIN_EDGE*100:.1f}%')}`"
    )
