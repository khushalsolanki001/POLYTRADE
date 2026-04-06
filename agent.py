"""
agent.py — Autonomous BTC 5-Minute Paper Trading Agent  (v5 — PROFESSIONAL HFT)
================================================================================

ARCHITECTURE:
  - Binance WS → real-time BTC price feed  (sub-second updates)
  - Every 1s (AGENT_POLL_SECONDS): evaluate momentum & scalp exits
  - FAST EXIT: Take Profit (+8%) and Stop Loss (-5%) triggers
  - SIGNAL REVERSAL: Close position if momentum flips hard against us
  - Dynamic Kelly Positioning: risk up to 10% of portfolio on high-edge signals

PROFESSIONAL RULES:
  1. Combined momentum: 1-min AND 3-min must agree in direction
  2. Aggressive Timing: enter 30s – 240s into the 5-min window
  3. Scalping: Exit early if target profit or loss limit hit
  4. Kelly-fraction position sizing (capped at 2x default or 10% balance)
  5. Per-window trade lock: only ONE trade per market slug ever
  6. Post-sell cooldown: 20s wait before re-evaluating any new entry
"""

import asyncio
import json
import logging
import os
import re
import time
import bisect
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Tuple, Set
from zoneinfo import ZoneInfo

import websockets  # type: ignore
from dotenv import load_dotenv  # type: ignore

import db  # type: ignore
import api  # type: ignore
from handlers import (  # type: ignore
    _paper_buy_core,
    _paper_sell_core,
    _get_target_market,
    _get_clob_price,
    _format_5m_et_label,
    _esc,
    _esc_code,
)

# Constants for direct API access
GAMMA_API_URL  = "https://gamma-api.polymarket.com/events?slug={slug}"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
AGENT_USER_ID: int      = int(os.getenv("AGENT_USER_ID",      "999999"))
_AGENT_CHAT_ID_ENV: int = int(os.getenv("AGENT_CHAT_ID",      "0"))
AGENT_TRADE_USD: float  = float(os.getenv("AGENT_TRADE_USD",  "25"))
AGENT_MIN_EDGE: float   = float(os.getenv("AGENT_MIN_EDGE",   "0.02"))   # 2% min (Aggressive)
AGENT_POLL_SECONDS: int = int(os.getenv("AGENT_POLL_SECONDS", "1"))      # HARD TRADING: 1s poll

COPY_SOURCE_WALLET = "0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82"

MAX_CONSECUTIVE_LOSSES = 5
PAUSE_DURATION         = 300    # 5-min cool-down after too many losses
MAX_DAILY_LOSS_PCT     = 0.20
STARTING_BALANCE       = 1000.0
MIN_PRICE_SAMPLES      = 10     # faster warmup

# Professional Exit Rules (Scalping)
TAKE_PROFIT_PCT = 0.08  # +8% ROI → Close early
STOP_LOSS_PCT   = 0.05  # -5% ROI → Close early
REVERSAL_EXIT   = True  # Exit if signal flips against us
KELLY_FRACTION  = 0.10  # Risk 10% of portfolio max per trade (Kelly-lite)

# Advanced Risk Settings
TSL_ACTIVATE_PCT = 0.04  # Activate trailing stop at +4% profit
TSL_TRAILING_PCT = 0.02  # Trailing distance (2%)
MAX_SPREAD_PCT   = 0.03  # Max allowed spread (3%) before entry
THETA_EXIT_SECS  = 265   # Exit if window is nearly closed and conditions met

WINDOW_SECS      = 300
SELL_RETRY_MAX   = 6     # max retries for sell
SELL_RETRY_DELAY = 30    # seconds between 'awaiting resolution' retries

# ─── Anti-duplicate guards ────────────────────────────────────────────────────
POST_SELL_COOLDOWN   = 15    # seconds to wait after closing a trade before new entry
SAME_WINDOW_LOCK     = True  # never re-enter the same slug twice in a session
MAX_HOLD_SECS        = 660   # 11 min — hard timeout: force-abandon stuck position

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
USER_TZ        = ZoneInfo("Asia/Kolkata")
MARKET_TZ      = ZoneInfo("America/New_York")

logger = logging.getLogger("PolyAgent")


# ─── Agent State ──────────────────────────────────────────────────────────────
class _State:
    enabled: bool              = True
    running: bool              = False
    prices: list               = []        # (unix_ts, btc_price)

    # Current open position tracking
    current_slug: Optional[str]       = None
    current_outcome: Optional[str]    = None
    current_buy_price: float          = 0.0
    current_shares: float             = 0.0
    current_btc_at_entry: float       = 0.0  # BTC spot price when we entered
    current_entry_time: float         = 0.0  # unix ts of entry
    current_entry_ist: str            = ""   # formatted IST string for display
    current_token_id: Optional[str]   = None # CLOB token ID for tracking ROI fast
    last_metadata_check: float        = 0.0  # throttle Gamma API calls (closed check)

    # Anti-duplicate: track which slugs we've already traded this session
    traded_slugs: Set[str]     = set()
    last_sell_time: float      = 0.0       # unix ts of last completed sell
    last_sell_attempt: float   = 0.0       # unix ts of last sell attempt (for retry throttle)

    # Risk counters
    consecutive_losses: int    = 0
    pause_until: float         = 0.0
    last_action: str           = "Not started"
    session_wins: int          = 0
    session_losses: int        = 0
    session_pnl: float         = 0.0
    daily_start_balance: Optional[float] = None
    last_day: Optional[object]           = None
    resolved_chat_id: int      = 0          # auto-detected chat_id
    sell_retry_count: int      = 0          # retry count for the current sell attempt
    tasks: list                = []

    # Sell-in-progress guard: prevent concurrent sell attempts
    sell_in_progress: bool     = False
    last_heartbeat: float      = 0.0
    
    # Speed Optimizations
    cached_balance: Optional[float] = None
    market_cache: Optional[dict]    = None
    market_cache_ts: float         = 0.0

    # Advanced Risk
    current_high_roi: float        = 0.0  # highest ROI reached during current trade (for TSL)
    trailing_stop_active: bool     = False


_s = _State()


# ─── Chat ID Resolution ───────────────────────────────────────────────────────

def _resolve_chat_id() -> int:
    if _AGENT_CHAT_ID_ENV:
        return _AGENT_CHAT_ID_ENV
    if _s.resolved_chat_id:
        return _s.resolved_chat_id
    try:
        import sqlite3 as _sq
        con = _sq.connect(db.DB_PATH)
        con.row_factory = _sq.Row
        row = con.execute("SELECT chat_id FROM users ORDER BY user_id LIMIT 1").fetchone()
        con.close()
        if row:
            _s.resolved_chat_id = int(row["chat_id"])
            return _s.resolved_chat_id
    except Exception:
        pass
    return 0


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _now_ist_str() -> str:
    return _now_utc().astimezone(USER_TZ).strftime("%d %b %I:%M:%S %p IST")

def _btc_now() -> float:
    return _s.prices[-1][1] if _s.prices else 0.0

def _btc_now_str() -> str:
    p = _btc_now()
    return f"${p:,.2f}" if p else "N/A"

def _get_balance() -> float:
    """Returns cached balance if available, else fetches from DB."""
    if _s.cached_balance is None:
        _s.cached_balance = float(db.get_paper_balance(AGENT_USER_ID))
    return _s.cached_balance

def _update_balance(new_bal: float) -> None:
    """Updates both DB and local cache."""
    db.update_paper_balance(AGENT_USER_ID, new_bal)
    _s.cached_balance = float(new_bal)

def _et_label(slug: str) -> str:
    try:
        return _format_5m_et_label(int(slug.split("-")[-1]))
    except Exception:
        return slug

def _window_age_secs(slug: str) -> Optional[float]:
    """How many seconds into the current 5-min window are we?"""
    try:
        window_start = int(slug.split("-")[-1])
        return time.time() - window_start
    except Exception:
        return None


# ─── Binance WS ───────────────────────────────────────────────────────────────

async def _binance_ws_loop() -> None:
    """Maintain persistent Binance WebSocket with exponential back-off."""
    backoff = 2.0
    while True:
        try:
            async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                logger.info("[AGENT] ✅ Binance WS connected — BTC price feed active")
                backoff = 2.0  # reset on successful connect
                while True:
                    raw  = await ws.recv()
                    data = json.loads(raw)
                    now  = time.time()
                    _s.prices.append((now, float(data["c"])))
                    # Sparse pruning to keep last 20 minutes of data for indicators
                    if len(_s.prices) % 100 == 0:
                        start_idx = 0
                        while start_idx < len(_s.prices) and now - _s.prices[start_idx][0] > 1200:
                            start_idx += 1
                        if start_idx > 0:
                            _s.prices = _s.prices[start_idx:]
        except asyncio.CancelledError:
            logger.info("[AGENT] Binance WS loop cancelled.")
            return
        except Exception as exc:
            logger.warning("[AGENT] Binance WS lost: %s — retry in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60)  # cap at 60s


# ─── Momentum Signal ──────────────────────────────────────────────────────────

def _price_n_secs_ago(secs: float) -> Optional[float]:
    if not _s.prices:
        return None
    target = _s.prices[-1][0] - secs
    
    # Binary search for performance (O(log N) instead of O(N))
    # We use bisect on timestamps. To avoid creating a full list in memory every time, 
    # we can use a wrapper or just slice, but for 2000 items, list creation is tiny.
    # Actually, a deque is not indexable for slicing, but we can do:
    # timestamps = [p[0] for p in _s.prices]
    # For HFT, let's just use a simple list instead of deque if we want fastest bisect,
    # or keep the small allocation. 2000 floats is very cheap.
    idx = bisect.bisect_right(_s.prices, (target, float('inf')))
    if idx > 0:
        return float(_s.prices[idx - 1][1])
    return float(_s.prices[0][1]) if _s.prices else None

def _compute_rsi(periods: int = 14) -> Optional[float]:
    """Calculate RSI based on 1-min increments."""
    if len(_s.prices) < (periods * 60):
        return None
    
    # Sample price changes every minute
    deltas = []
    for i in range(1, periods + 1):
        p_now = _price_n_secs_ago((i-1)*60)
        p_prev = _price_n_secs_ago(i*60)
        if p_now and p_prev:
            deltas.append(p_now - p_prev)
            
    if not deltas:
        return None
        
    gains = [d for d in deltas if d > 0]
    losses = [abs(d) for d in deltas if d < 0]
    
    avg_gain = sum(gains) / periods
    avg_loss = sum(losses) / periods
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def _compute_signal() -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (momentum_p, edge, raw_factor). 1-min and 3-min must AGREE."""
    if len(_s.prices) < MIN_PRICE_SAMPLES:
        return None, None, None

    now_p = float(_s.prices[-1][1])
    p1    = _price_n_secs_ago(60)
    p3    = _price_n_secs_ago(180)

    if not p1 or not p3:
        return None, None, None

    mom1 = (now_p - p1) / p1 if p1 else 0.0
    mom3 = (now_p - p3) / p3 if p3 else 0.0

    # RSI Filter: avoid buying at tops or selling at bottoms
    rsi = _compute_rsi(10)
    if rsi is not None:
        if mom1 > 0 and rsi > 80: # extremely overbought
            return None, None, float(mom1)
        if mom1 < 0 and rsi < 20: # extremely oversold
            return None, None, float(mom1)

    # Both must agree in direction and have minimum velocity
    if (mom1 >= 0) != (mom3 >= 0):
        return None, None, float(mom1)
    
    # Velocity threshold: require at least 0.01% movement in 1min
    if abs(mom1) < 0.0001:
        return None, None, float(mom1)

    # Momentum is EMA-like weighting
    # factor = (Weighted Avg of Mom1 and Mom3)
    factor     = (mom1 * 15.0) + (mom3 * 5.0)
    
    # Sigmoid-ish probability centering at 0.5
    momentum_p = max(0.1, min(0.9, 0.5 + factor))
    edge       = abs(momentum_p - 0.5)

    return momentum_p, edge, factor


# ─── Telegram Notifications ────────────────────────────────────────────────────

async def _send(bot, text: str) -> None:
    chat_id = _resolve_chat_id()
    if not chat_id:
        return
    try:
        target = getattr(bot, "bot", bot)
        await target.send_message(
            chat_id=chat_id, text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            target = getattr(bot, "bot", bot)
            plain = re.sub(r"[*_`\[\]()~>#+=|\\]", "", text)
            await target.send_message(chat_id=chat_id, text=plain)
        except Exception as e2:
            logger.warning("[AGENT] TG send failed: %s", e2)


def _fmt_startup(balance: float) -> str:
    btc = _btc_now_str()
    src = f"`{_AGENT_CHAT_ID_ENV}`" if _AGENT_CHAT_ID_ENV else "auto\\-detected"
    return (
        "🤖 *PolyAgent v5 — ONLINE \\(PROFESSIONAL HFT\\)*\n\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"⚡ *Min edge:* `{_esc_code(f'{AGENT_MIN_EDGE*100:.0f}%')}`  "
        f"\\|  *Trade:* `Kelly 10%`  "
        f"\\|  *Poll:* `{AGENT_POLL_SECONDS}s`\n"
        f"📈 *Take Profit:* `+8%`  \\|  *Stop Loss:* `-5%`\n"
        f"🔒 *Window lock:* ON \\| *Cooldown:* `{POST_SELL_COOLDOWN}s`\n"
        f"₿ *BTC now:* `{_esc_code(btc)}`  \\|  📬 {src}\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )

def _fmt_buy_alert(outcome: str, fill_price: float, edge: float,
                   momentum_p: float, slug: str, mom1: float, mom3: float, trade_usd: float) -> str:
    direction_emoji = "🟢" if outcome.lower() == "up" else "🔴"
    balance  = db.get_paper_balance(AGENT_USER_ID)
    btc_now  = _btc_now()
    shares   = trade_usd / fill_price if fill_price > 0 else 0
    return (
        f"🤖 *AUTO BUY — {direction_emoji} {_esc(outcome.upper())}*\n\n"
        f"📊 *Market:* BTC {_esc(_et_label(slug))}\n"
        f"💰 *Entry:* `${_esc_code(f'{fill_price:.4f}')}` \\| *Amt:* `${_esc_code(f'{trade_usd:.2f}')}`\n"
        f"📦 *Shares:* `{_esc_code(f'{shares:.2f}')}` \\| ₿ BTC: `{_esc_code(f'${btc_now:,.2f}')}`\n"
        f"⚡ *Mom:* 1m `{_esc_code(f'{mom1*100:+.2f}%')}` 3m `{_esc_code(f'{mom3*100:+.2f}%')}`\n"
        f"📈 *Edge:* `{_esc_code(f'{edge*100:.2f}%')}` \\| *Conf:* `{_esc_code(f'{momentum_p*100:.1f}%')}`\n"
        f"💵 *New Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )

def _fmt_sell_alert(outcome: str, sell_price: float, buy_price: float,
                    shares: float, pnl: float, slug: str,
                    btc_entry: float, btc_exit: float, entry_ist: str) -> str:
    # Determine result label
    if pnl > 0.01:
        result_text = "✅ *WIN / TP*"
        pnl_sign = "+"
    elif pnl < -0.01:
        result_text = "🛑 *STOP LOSS*"
        pnl_sign = "\\-"
    else:
        result_text = "⚖️ *NEUTRAL*"
        pnl_sign = ""

    roi     = (pnl / (buy_price * shares) * 100) if (buy_price * shares > 0) else 0
    balance = _get_balance()

    return (
        f"🤖 *AUTO SELL — {result_text}*\n\n"
        f"📊 *Market:* BTC {_esc(_et_label(slug))}  Dir: *{_esc(outcome.upper())}*\n"
        f"💰 *Price:* `${_esc_code(f'{buy_price:.4f}')}` → `${_esc_code(f'{sell_price:.4f}')}`\n"
        f"💵 *PnL:* `{pnl_sign}${_esc_code(f'{abs(float(pnl)):.2f}')}` \\({_esc_code(f'{pnl_sign}{abs(float(roi)):.1f}%')} ROI\\)\n"
        f"₿ *BTC:* `{_esc_code(f'${btc_entry:,.0f}')}` → `{_esc_code(f'${btc_exit:,.0f}')}`\n"
        f"🏆 *Session:* `{_s.session_wins}W` / `{_s.session_losses}L`  Bal: `${_esc_code(f'{balance:.2f}')}`\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )

def _fmt_skipped(reason: str, momentum_p: Optional[float],
                 edge: Optional[float], mom1: Optional[float], mom3: Optional[float]) -> str:
    return f"SKIP [{reason}] btc={_btc_now_str()} p={momentum_p or 0:.1%} e={edge or 0:.1%}"


# ─── Orphan Recovery ──────────────────────────────────────────────────────────

def _recover_orphaned_position() -> None:
    """
    On startup: if the DB has an open agent position from a previous run,
    re-register it in _s so the auto-sell logic can close it properly.
    Also restores recently traded slugs to prevent immediate re-entry.
    """
    try:
        # 1. Recover recently traded slugs to prevent duplicate entry after restart
        recent_slugs = db.get_recent_traded_slugs(AGENT_USER_ID, limit=50)
        _s.traded_slugs.update(recent_slugs)
        if recent_slugs:
            logger.info("[AGENT] Recovered %d recently traded slugs from DB", len(recent_slugs))

        # 2. Recover live position
        positions = db.get_all_paper_positions(AGENT_USER_ID)
        if positions:
            p = positions[0]  # take the most recent
            _s.current_slug       = p["market_slug"]
            _s.current_outcome    = p["outcome"]
            _s.current_buy_price  = p["avg_price"]
            _s.current_shares     = p["shares"]
            _s.current_btc_at_entry = 0.0   # unknown — lost on restart
            _s.current_entry_ist  = "prev session"
            _s.current_entry_time = 0.0
            # Add slug to traded set to prevent re-entering even after sell
            _s.traded_slugs.add(p["market_slug"])
            logger.warning(
                "[AGENT] Recovered orphaned position from DB: %s %s @ %.4f",
                p["outcome"], p["market_slug"], p["avg_price"]
            )
    except Exception as exc:
        logger.error("[AGENT] Orphan recovery error: %s", exc)


# ─── Parse Fill Price from buy message ───────────────────────────────────────

def _parse_fill_price_from_msg(msg: str, fallback: float = 0.5) -> float:
    """
    Robustly extract the actual fill price from the _paper_buy_core message.
    Tries multiple patterns to handle message formatting variations.
    """
    clean = str(msg).replace("\\", "").replace("`", "").replace("*", "")

    # Pattern 1: "Buy price: $0.6900"
    m = re.search(r"buy\s+price[:\s]+\$?([0-9]+\.[0-9]+)", clean, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # Pattern 2: "@ $0.6900"
    m = re.search(r"@\s*\$?([0-9]+\.[0-9]+)", clean)
    if m:
        try:
            val = float(m.group(1))
            if 0.01 < val < 1.0:
                return val
        except ValueError:
            pass

    # Pattern 3: any dollar amount between 0.01 and 0.99
    matches = re.findall(r"\$([0-9]+\.[0-9]{3,4})", clean)
    for raw in matches:
        try:
            val = float(raw)
            if 0.01 < val < 1.0:
                return val
        except ValueError:
            continue

    logger.warning("[AGENT] Could not parse fill price, using fallback %.4f. Msg: %s", fallback, clean[:200])
    return fallback


# ─── Auto-Sell ────────────────────────────────────────────────────────────────

def _force_clear_position(reason: str) -> None:
    """
    Unconditionally clear all position state.
    Called when a position is truly stuck and cannot be sold.
    """
    logger.critical(
        "[AGENT] 🆘 FORCE-CLEARING stuck position: %s %s — reason: %s",
        _s.current_outcome, _s.current_slug, reason,
    )
    _s.current_slug     = None
    _s.current_outcome  = None
    _s.sell_in_progress = False
    _s.sell_retry_count = 0
    _s.last_sell_time   = time.time()


async def _attempt_sell(
    bot, slug: str, outcome: str,
    buy_price: float, shares: float,
    btc_entry: float, entry_ist: str,
    retry: int = 0,
) -> bool:
    """
    Attempts to paper-sell the current position.
    Returns True  → sell succeeded or hard-failed (position cleared)
    Returns False → sell failed with 'awaiting resolution' (throttled retry later)
    """
    logger.info("[AGENT] Sell attempt: %s in %s (try #%d)", outcome, slug, retry + 1)
    _s.last_sell_attempt = time.time()

    try:
        ok, msg = await _paper_sell_core(AGENT_USER_ID, outcome, None, slug)
    except Exception as exc:
        logger.error("[AGENT] _paper_sell_core raised: %s", exc)
        # Don't get stuck — treat as retriable
        _s.sell_in_progress = False
        return False

    msg_str = str(msg).replace("\\", "").replace("`", "").replace("*", "")

    if ok:
        # Parse sell price from the response message
        sell_price = buy_price  # safe fallback
        m = re.search(r"@\s*\$?([0-9]+\.[0-9]+)", msg_str)
        if m:
            try:
                sell_price = float(m.group(1))
            except ValueError:
                pass
        else:
            # Try "Price: $x.xxxx → $y.yyyy" pattern
            m2 = re.search(r"→\s*\$?([0-9]+\.[0-9]+)", msg_str)
            if m2:
                try:
                    sell_price = float(m2.group(1))
                except ValueError:
                    pass

        pnl = (sell_price - buy_price) * shares
        if pnl >= 0:
            _s.session_wins += 1
            _s.consecutive_losses = 0
        else:
            _s.session_losses += 1
            _s.consecutive_losses += 1

        _s.session_pnl += pnl
        
        # Sync balance to cache after trade
        _s.cached_balance = float(db.get_paper_balance(AGENT_USER_ID))

        # Apply consecutive loss pause
        if _s.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            _s.pause_until = time.time() + PAUSE_DURATION
            logger.warning("[AGENT] %d consecutive losses → pausing %ds", _s.consecutive_losses, PAUSE_DURATION)

        # ── Clear position state and record sell time ──────────────────────
        closed_slug    = _s.current_slug
        closed_outcome = _s.current_outcome
        _s.current_slug     = None
        _s.current_outcome  = None
        _s.sell_in_progress = False
        _s.sell_retry_count = 0
        _s.last_sell_time   = time.time()

        await _send(bot, _fmt_sell_alert(
            outcome, sell_price, buy_price, shares, pnl,
            slug, btc_entry, _btc_now(), entry_ist,
        ))
        logger.info(
            "[AGENT] ✅ Position closed: %s %s  PnL=$%.2f  Cooldown=%ds",
            closed_outcome, closed_slug, pnl, POST_SELL_COOLDOWN,
        )
        return True

    # Sell failed
    _s.sell_in_progress = False
    _s.sell_retry_count += 1

    if "awaiting" in msg_str.lower():
        logger.info(
            "[AGENT] Market awaiting resolution — retry #%d (next in %ds)",
            _s.sell_retry_count, SELL_RETRY_DELAY,
        )
        return False  # Retriable — throttled by caller

    # Hard error (e.g. no shares found in DB) — clear position to unblock
    logger.error("[AGENT] Hard sell error (clearing position): %s", msg_str[:200])
    _force_clear_position("hard sell error")
    return True


# ─── Scalping Exit Logic ──────────────────────────────────────────────────────

async def _check_scalp_exit(bot) -> bool:
    """
    Check if the current position should be exited via TP/SL/Reversal/Expiry.
    Returns True if the position was closed or is being closed.
    Returns False if still holding (no exit condition met yet).

    CRITICAL DESIGN: sell_in_progress is ALWAYS reset in a finally block
    so it can never get permanently stuck if an exception occurs.
    """
    if not _s.current_slug or not _s.current_outcome:
        return False

    slug    = str(_s.current_slug)
    outcome = str(_s.current_outcome)
    now_ts  = time.time()

    # ── Hard position timeout: force-abandon if stuck too long ───────────────
    hold_secs = now_ts - _s.current_entry_time
    if _s.current_entry_time > 0 and hold_secs > MAX_HOLD_SECS:
        logger.error(
            "[AGENT] ⚠️ Position held %.0fs (> %ds max) — force-abandoning %s %s",
            hold_secs, MAX_HOLD_SECS, outcome, slug,
        )
        _force_clear_position(f"max hold time exceeded ({hold_secs:.0f}s)")
        await _send(bot,
            f"⚠️ *PolyAgent: Position force\\-abandoned*\n"
            f"Held {round(hold_secs/60, 1)} min \\(max {MAX_HOLD_SECS//60} min\\)\n"
            f"Slug: `{_esc_code(slug[-30:])}`\n"
            f"Outcome: *{_esc(outcome)}*"
        )
        return True

    # ── Guard against concurrent sell attempts ────────────────────────────────
    if _s.sell_in_progress:
        # Watchdog: if sell_in_progress has been True for > 30s something silently
        # failed — reset it so exit logic can retry
        if now_ts - _s.last_sell_attempt > 30:
            logger.warning("[AGENT] sell_in_progress stuck >30s — resetting")
            _s.sell_in_progress = False
        else:
            return True

    # ── Throttle 'awaiting resolution' retries ────────────────────────────────
    age = _window_age_secs(slug)
    window_expired = (age is not None and age > WINDOW_SECS + 30)

    # Only retry an awaiting-resolution sell every SELL_RETRY_DELAY seconds
    if _s.sell_retry_count > 0:
        secs_since_attempt = now_ts - _s.last_sell_attempt
        if secs_since_attempt < SELL_RETRY_DELAY:
            return True  # Still holding — wait for throttle

    async def _do_sell() -> bool:
        """Inner helper — sell_in_progress is managed by the outer try/finally."""
        return await _attempt_sell(
            bot, slug, outcome,
            _s.current_buy_price, _s.current_shares,
            _s.current_btc_at_entry, _s.current_entry_ist,
            retry=_s.sell_retry_count,
        )

    # ── API check + exit logic ────────────────────────────────────────────────
    try:
        # Check metadata (closed status) only every 5 seconds OR if token_id is missing
        needs_metadata = (_s.current_token_id is None or (now_ts - _s.last_metadata_check > 5))
        
        session = await api.get_session()
        
        if needs_metadata:
            _s.last_metadata_check = now_ts
            async with session.get(GAMMA_API_URL.format(slug=slug)) as r:
                if r.status != 200:
                    if window_expired:
                        logger.warning("[AGENT] Window expired + API unreachable → force sell")
                        _s.sell_in_progress = True
                        try:
                            return await _do_sell()
                        finally:
                            _s.sell_in_progress = False
                    return False

                data    = await r.json()
                markets = data[0].get("markets", []) if data else []
                if not markets:
                    return False

                m = markets[0]

                # ── Market resolved → sell immediately ────────────────────
                if m.get("closed") or m.get("active") is False:
                    logger.info("[AGENT] Market resolved → sell")
                    _s.sell_in_progress = True
                    try:
                        return await _do_sell()
                    finally:
                        _s.sell_in_progress = False

                # Extract token ID if not already known
                if _s.current_token_id is None and m.get("clobTokenIds"):
                    tids = json.loads(m["clobTokenIds"])
                    idx  = 0 if outcome.lower() in ("up", "yes") else 1
                    if tids and idx < len(tids):
                        _s.current_token_id = str(tids[idx])

        # ── CLOB price → HFT scalp exit ───────────────────────────
        if _s.current_token_id:
            cp = await _get_clob_price(session, _s.current_token_id, "buy")
            if cp is not None and cp > 0:
                roi = (cp - _s.current_buy_price) / _s.current_buy_price
                logger.info(
                    "[AGENT] 📊 Hold check: %s ROI=%+.2f%% now=%.4f",
                    outcome, roi * 100, cp,
                )

                # ── TSL & ROBUST EXIT LOGIC ──
                _s.current_high_roi = max(_s.current_high_roi, roi)
                
                # Take Profit (Hard target)
                if roi >= TAKE_PROFIT_PCT:
                    logger.info("[AGENT] 📈 TAKE PROFIT: ROI=%.1f%%", roi * 100)
                    _s.sell_in_progress = True
                    try: return await _do_sell()
                    finally: _s.sell_in_progress = False

                # Trailing Stop Loss Activation
                if not _s.trailing_stop_active and roi >= TSL_ACTIVATE_PCT:
                    _s.trailing_stop_active = True
                    logger.info("[AGENT] 🛡️ Trailing Stop Activated at ROI=%.1f%%", roi * 100)

                if _s.trailing_stop_active:
                    # Move stop to (High - Trailing Dist)
                    tsl_exit_threshold = _s.current_high_roi - TSL_TRAILING_PCT
                    # Floor at break-even once TSL is active
                    tsl_exit_threshold = max(0.005, tsl_exit_threshold) # +0.5% buffer for fees
                    
                    if roi <= tsl_exit_threshold:
                        logger.info("[AGENT] 📉 TRAILING STOP HIT: ROI=%.1f%% (High was %.1f%%)", roi * 100, _s.current_high_roi * 100)
                        _s.sell_in_progress = True
                        try: return await _do_sell()
                        finally: _s.sell_in_progress = False

                # Hard Stop Loss (fallback)
                if not _s.trailing_stop_active and roi <= -STOP_LOSS_PCT:
                    logger.info("[AGENT] 🛑 STOP LOSS: ROI=%.1f%%", roi * 100)
                    _s.sell_in_progress = True
                    try: return await _do_sell()
                    finally: _s.sell_in_progress = False

                # ── Time-Decay (Theta) Exit ──
                # If window is ending soon, lock in any winner or cut a bad loser
                if age is not None and age > THETA_EXIT_SECS:
                    if roi > 0.01:
                        logger.info("[AGENT] ⌛ THETA EXIT (Profit Lock): ROI=%.1f%%", roi * 100)
                        _s.sell_in_progress = True
                        try: return await _do_sell()
                        finally: _s.sell_in_progress = False
                    elif roi < -0.03:
                        logger.info("[AGENT] ⌛ THETA EXIT (Loss Cut): ROI=%.1f%%", roi * 100)
                        _s.sell_in_progress = True
                        try: return await _do_sell()
                        finally: _s.sell_in_progress = False

                # ── Reversal exit ─────────────────────────────
                if REVERSAL_EXIT:
                    mp, _, _ = _compute_signal()
                    if mp is not None:
                        is_long  = outcome.lower() == "up"
                        # Looser reversal to avoid noise — exit if prob flips > 15% against us
                        reversal = (is_long and mp < 0.35) or (not is_long and mp > 0.65)
                        if reversal:
                            logger.info("[AGENT] 🔄 REVERSAL EXIT: mp=%.3f", mp)
                            _s.sell_in_progress = True
                            try:
                                return await _do_sell()
                            finally:
                                _s.sell_in_progress = False

    except asyncio.CancelledError:
        _s.sell_in_progress = False
        raise
    except Exception as exc:
        _s.sell_in_progress = False
        logger.error("[AGENT] Scalp exit error: %s", exc)
        if window_expired:
            logger.warning("[AGENT] Window expired + error → force sell")
            _s.sell_in_progress = True
            try:
                return await _do_sell()
            finally:
                _s.sell_in_progress = False

    return False


# ─── Main Cycle ───────────────────────────────────────────────────────────────

async def _cycle(bot) -> None:
    """One evaluation tick of the agent. Called every AGENT_POLL_SECONDS."""

    if not _s.enabled or time.time() < _s.pause_until:
        return

    # Heartbeat (once per minute)
    now_ts = time.time()
    if int(now_ts) % 60 == 0 and now_ts - _s.last_heartbeat >= 50:
        _s.last_heartbeat = now_ts
        bal = _get_balance()
        mp, edge, _ = _compute_signal()
        logger.info(
            "[AGENT] ❤️ Tick: holding=%s portfolio=$%.2f signal=%s edge=%s cooldown=%ds",
            _s.current_slug or "None", bal,
            f"{mp:.3f}" if mp else "N/A",
            f"{edge:.1%}" if edge else "N/A",
            max(0, int(_s.last_sell_time + POST_SELL_COOLDOWN - now_ts)),
        )

    # Daily risk check
    today   = _now_utc().date()
    balance = _get_balance()
    if _s.last_day != today:
        _s.daily_start_balance = balance
        _s.last_day = today
    if _s.daily_start_balance and (
        (balance - _s.daily_start_balance) / _s.daily_start_balance <= -MAX_DAILY_LOSS_PCT
    ):
        return  # daily loss-limit hit

    # ── Currently holding a position → check exits ────────────────────────────
    if _s.current_slug and _s.current_outcome:
        await _check_scalp_exit(bot)
        return  # skip entry logic while holding

    # ── Post-sell cooldown guard ───────────────────────────────────────────────
    secs_since_sell = now_ts - _s.last_sell_time
    if _s.last_sell_time > 0 and secs_since_sell < POST_SELL_COOLDOWN:
        logger.debug(
            "[AGENT] Post-sell cooldown: %.1fs remaining",
            POST_SELL_COOLDOWN - secs_since_sell,
        )
        return

    # ── Signal Evaluation (not currently holding) ─────────────────────────────
    # Cache market metadata for 20s to avoid hammering Gamma API
    if _s.market_cache and (now_ts - _s.market_cache_ts < 20):
        market, mode = _s.market_cache["market"], _s.market_cache["mode"]
    else:
        market, mode = await _get_target_market()
        _s.market_cache = {"market": market, "mode": mode}
        _s.market_cache_ts = now_ts
        
    if not market:
        logger.debug("[AGENT] No tradeable market found — skip")
        return

    slug = str(market.get("slug", ""))

    # ── Window-lock: never trade the same slug twice in a session ────────────
    if SAME_WINDOW_LOCK and slug in _s.traded_slugs:
        logger.info("[AGENT] 🔒 Window already traded: %s", slug[-25:])
        return

    # Entry timing: 30s – 265s into the window (leaves ~35s minimum hold before expiry)
    age = _window_age_secs(slug)
    if age is None:
        logger.info("[AGENT] Window age unknown for %s — skip", slug[-25:])
        return
    if age < 30:
        logger.debug("[AGENT] Too early to enter: %.0fs into window", age)
        return
    if age > 265:
        logger.info(
            "[AGENT] ⏰ Window too old (%.0fs elapsed, %.0fs remaining) — skip entry",
            age, WINDOW_SECS - age,
        )
        return

    m_prob, e_val, raw_factor = _compute_signal()
    if m_prob is None or e_val is None:
        logger.info(
            "[AGENT] ⚡ Signal conflict: 1m/3m disagree — skip  btc=%s",
            _btc_now_str(),
        )
        return
    m_p, edge = float(m_prob), float(e_val)
    if edge < AGENT_MIN_EDGE:
        logger.info(
            "[AGENT] ⚡ Edge too low: %.2f%% < %.2f%% — skip",
            edge * 100, AGENT_MIN_EDGE * 100,
        )
        return

    # Momentum metrics for alert
    p1v   = _price_n_secs_ago(60)
    p3v   = _price_n_secs_ago(180)
    now_p = _btc_now()
    m1    = (now_p - p1v) / p1v if p1v else 0.0
    m3    = (now_p - p3v) / p3v if p3v else 0.0

    # Dynamic Kelly Sizing based on Edge strength
    base_amt = AGENT_TRADE_USD
    # Multiplier: if edge is 2x AGENT_MIN_EDGE, we double the trade size (up to 3x cap)
    multiplier = edge / AGENT_MIN_EDGE
    multiplier = max(1.0, min(3.0, multiplier))
    
    t_amt = base_amt * multiplier
    if balance > STARTING_BALANCE:
        # Scale with portfolio but cap at 10% or 3x base
        t_amt = min(AGENT_TRADE_USD * 3, balance * KELLY_FRACTION, t_amt)

    outcome = "Up" if m_p > 0.5 else "Down"

    # ── Spread Check ──
    try:
        session = await api.get_session()
        m_data = await _get_target_market(slug)
        if m_data and m_data[0]:
            t_ids = json.loads(m_data[0].get("clobTokenIds", "[]"))
            outcomes = json.loads(m_data[0].get("outcomes", "[]"))
            idx = 0 if outcome.lower() == "up" else 1
            if t_ids and idx < len(t_ids):
                ask = await _get_clob_price(session, t_ids[idx], "sell")
                bid = await _get_clob_price(session, t_ids[idx], "buy")
                if ask and bid:
                    spread = (ask - bid) / ((ask + bid) / 2)
                    if spread > MAX_SPREAD_PCT:
                        logger.info("[AGENT] 🛡️ Spread too wide: %.1f%% > %.1f%% — skip", spread * 100, MAX_SPREAD_PCT * 100)
                        return
    except Exception as e:
        logger.warning("[AGENT] Spread check failed: %s", e)

    logger.info(
        "[AGENT] 🔔 Signal → %s  edge=%.2f%%  mom1=%.2f%%  mom3=%.2f%%  slug=%s",
        outcome, edge * 100, m1 * 100, m3 * 100, slug,
    )

    ok, msg, returned_slug = await _paper_buy_core(AGENT_USER_ID, outcome, t_amt, slug)

    if ok:
        # ── Register position in state ────────────────────────────────────
        _s.current_slug    = slug
        _s.current_outcome = outcome

        # Parse fill price robustly
        fill = _parse_fill_price_from_msg(str(msg), fallback=0.5)
        _s.current_buy_price    = fill
        _s.current_shares       = t_amt / fill if fill > 0 else t_amt
        _s.current_btc_at_entry = now_p
        _s.current_entry_ist    = _now_ist_str()
        _s.current_entry_time   = now_ts
        _s.sell_in_progress     = False
        _s.sell_retry_count     = 0
        _s.current_token_id     = None # will be set on first check
        _s.last_metadata_check  = 0.0
        _s.current_high_roi     = 0.0
        _s.trailing_stop_active = False
        
        # Sync balance after buy
        _s.cached_balance = float(db.get_paper_balance(AGENT_USER_ID))

        # ── Lock this slug from being re-entered ──────────────────────────
        _s.traded_slugs.add(slug)

        await _send(bot, _fmt_buy_alert(outcome, fill, edge, m_p, slug, m1, m3, t_amt))
        logger.info(
            "[AGENT] ✅ BUY OK: %s %s @ %.4f  shares=%.2f  slug=%s",
            outcome, slug, fill, _s.current_shares, slug,
        )
    else:
        logger.warning("[AGENT] Buy failed: %s", str(msg)[:200])


# ─── Public API ────────────────────────────────────────────────────────────────
#some time loop happen here

def toggle() -> bool:
    _s.enabled = not _s.enabled
    logger.info("[AGENT] Bot %s", "ENABLED" if _s.enabled else "PAUSED")
    return _s.enabled

def is_running() -> bool:
    return _s.running

async def start(bot):
    if _s.running:
        return
    _s.running = True
    _recover_orphaned_position()
    loop = asyncio.get_running_loop()
    _s.tasks.append(loop.create_task(_binance_ws_loop()))

    async def _main_loop():
        await _send(bot, _fmt_startup(db.get_paper_balance(AGENT_USER_ID)))
        while _s.running:
            try:
                await _cycle(bot)
            except asyncio.CancelledError:
                logger.info("[AGENT] Main loop cancelled cleanly.")
                return
            except Exception as exc:
                # Never let the loop die from an unexpected error
                logger.error("[AGENT] 💥 Unhandled error in _cycle: %s", exc, exc_info=True)
            await asyncio.sleep(AGENT_POLL_SECONDS)

    _s.tasks.append(loop.create_task(_main_loop()))
    logger.info("[AGENT] Started v5 (Professional HFT Mode)")

def stop():
    _s.running = False
    for t in _s.tasks:
        t.cancel()
    _s.tasks = []
    
    # Sync shutdown session
    try:
        import api
        asyncio.create_task(api.close_session())
    except Exception:
        pass
    
    logger.info("[AGENT] Stopped")

def get_status_message() -> str:
    balance = _get_balance()
    pnl     = balance - STARTING_BALANCE
    psign   = "+" if pnl >= 0 else ""
    now_ts  = time.time()
    cooldown_left = max(0, int(_s.last_sell_time + POST_SELL_COOLDOWN - now_ts))
    holding_info = (
        f"📌 *Holding:* `{_esc_code(str(_s.current_outcome))}` in `{_esc_code(str(_s.current_slug or 'None')[-20:])}`\n"
        if _s.current_slug else ""
    )
    lock_info = (
        f"🔒 *Slugs locked:* `{len(_s.traded_slugs)}`\n"
        f"⏳ *Cooldown left:* `{cooldown_left}s`\n"
        if not _s.current_slug else ""
    )
    return (
        f"🤖 *PolyAgent v5 — Status*\n\n"
        f"{'🟢 ACTIVE' if _s.enabled else '🔴 PAUSED'}\n"
        f"📡 *BTC:* `{_esc_code(_btc_now_str())}`\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}` \\({psign}${_esc_code(f'{pnl:.2f}')}\\)\n"
        f"🏆 *Session:* `{_s.session_wins}W` / `{_s.session_losses}L`\n"
        f"{holding_info}"
        f"{lock_info}"
        f"📝 *Last:* {_esc(str(_s.last_action)[:50])}\n"
        f"⚙️ *HFT:* Polling 1s \\| TP 8% \\| SL 5% \\| Cooldown {POST_SELL_COOLDOWN}s\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )

async def process_copy_trade(wallet: str, trade_type: str, outcome: str, usd_value: float, title: str, slug: str, app) -> None:
    """
    Called by scanner when the tracked copy-trade wallet makes a trade.
    Executes an arbitrage/copy paper trade mimicking the source.
    """
    if wallet.lower() != COPY_SOURCE_WALLET.lower():
        return
        
    if not _s.enabled or not _s.running:
        return
        
    chat_id = _resolve_chat_id()
    if not chat_id:
        return
        
    logger.info(f"[COPY-AGENT] Detected trade from {COPY_SOURCE_WALLET}: {trade_type} {outcome} (Slug: {slug})")
    
    # Determine sizing: scale proportionally or use our fixed agent amount
    # We will use the agent's base trade size for safety.
    amount_to_trade = AGENT_TRADE_USD
    
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"🤖 *Agent Copy Trade Detected*\nSource: `{COPY_SOURCE_WALLET[:8]}...`\nAction: {trade_type} *{_esc(outcome)}*\nMarket: {_esc(title)}\n_Executing copy trade..._",
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.warning(f"Could not send copy trade alert: {e}")
    
    try:
        if trade_type == "BUY":
            ok, msg, _ = await _paper_buy_core(AGENT_USER_ID, outcome, amount_to_trade, slug_override=slug)
        else:
            # Sell our position for this outcome/slug
            # amount_key=None means sell all shares for this outcome
            ok, msg = await _paper_sell_core(AGENT_USER_ID, outcome, amount_key=None, slug_override=slug)
            
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"✅ *Copy Trade Executed:*\n{msg}\n_Arbitrage synced successfully\\._",
                parse_mode="MarkdownV2"
            )
        except Exception:
            plain_msg = msg.replace('*', '').replace('`', '').replace('\\', '')
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Copy Trade Executed:\n{plain_msg}"
            )
    except Exception as e:
        logger.error(f"[COPY-AGENT] Execution failed: {e}")
        try:
            await app.bot.send_message(chat_id=chat_id, text=f"❌ Agent copy trade failed: {e}")
        except Exception:
            pass
