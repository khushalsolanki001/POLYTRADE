"""
agent.py — Autonomous BTC 5-Minute Paper Trading Agent  (v4 — HARD TRADING / HFT)
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
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional, Tuple, Any
from zoneinfo import ZoneInfo

import aiohttp # type: ignore
import websockets # type: ignore
from dotenv import load_dotenv # type: ignore

import db # type: ignore
from handlers import ( # type: ignore
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

MAX_CONSECUTIVE_LOSSES = 5
PAUSE_DURATION         = 300    # 5-min cool-down
MAX_DAILY_LOSS_PCT     = 0.20
STARTING_BALANCE       = 1000.0
MIN_PRICE_SAMPLES      = 10     # faster warmup

# Professional Exit Rules (Scalping)
TAKE_PROFIT_PCT = 0.08  # +8% ROI → Close early
STOP_LOSS_PCT   = 0.05  # -5% ROI → Close early
REVERSAL_EXIT   = True  # Exit if signal flips against us
KELLY_FRACTION  = 0.10  # Risk 10% of portfolio max per trade (Kelly-lite)

WINDOW_SECS      = 300
SELL_RETRY_MAX   = 6     # max retries for sell
SELL_RETRY_DELAY = 10    # seconds between retries (faster)

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
    while True:
        try:
            async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                logger.info("[AGENT] ✅ Binance WS connected — BTC price feed active")
                while True:
                    raw  = await ws.recv()
                    data = json.loads(raw)
                    now  = time.time()
                    _s.prices.append((now, float(data["c"])))
                    _s.prices = [(t, p) for t, p in _s.prices if now - t < 300]
        except Exception as exc:
            logger.warning("[AGENT] Binance WS lost: %s — retry in 5s", exc)
            await asyncio.sleep(5)


# ─── Momentum Signal ──────────────────────────────────────────────────────────

def _price_n_secs_ago(secs: float) -> Optional[float]:
    if not _s.prices:
        return None
    target = _s.prices[-1][0] - secs
    for t, p in reversed(_s.prices):
        if t <= target:
            return float(p)
    return float(_s.prices[0][1])

def _compute_signal() -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (momentum_p, edge, raw_factor). 1-min and 3-min must AGREE."""
    if len(_s.prices) < MIN_PRICE_SAMPLES:
        return None, None, None

    now_p = float(_s.prices[-1][1])
    p1    = _price_n_secs_ago(60)
    p3    = _price_n_secs_ago(180)

    if not p1 or not p3:
        return None, None, None

    mom1 = (now_p - p1) / p1
    mom3 = (now_p - p3) / p3

    if (mom1 >= 0) != (mom3 >= 0):
        return None, None, float(mom1)

    factor     = (mom1 * 12) + (mom3 * 8)
    momentum_p = max(0.05, min(0.95, 0.5 + factor))
    edge       = abs(momentum_p - 0.5)

    return momentum_p, edge, factor


# ─── Telegram Notifications ────────────────────────────────────────────────────

async def _send(bot, text: str) -> None:
    chat_id = _resolve_chat_id()
    if not chat_id: return
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="MarkdownV2", disable_web_page_preview=True)
    except Exception:
        try:
            plain = re.sub(r"[*_`\[\]()~>#+=|\\]", "", text)
            await bot.send_message(chat_id=chat_id, text=plain)
        except Exception as e2:
            logger.warning("[AGENT] TG send failed: %s", e2)


def _fmt_startup(balance: float) -> str:
    btc = _btc_now_str()
    src = f"`{_AGENT_CHAT_ID_ENV}`" if _AGENT_CHAT_ID_ENV else "auto\\-detected"
    return (
        "🤖 *PolyAgent v4 — ONLINE (HARD TRADING)*\n\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"⚡ *Min edge:* `{_esc_code(f'{AGENT_MIN_EDGE*100:.0f}%')}`  "
        f"\\|  *Trade:* `Kelly 10%`  "
        f"\\|  *Poll:* `{AGENT_POLL_SECONDS}s`\n"
        f"📈 *Take Profit:* `+8%`  \\|  *Stop Loss:* `-5%`\n"
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
    win         = pnl >= 0
    result_text = "✅ *WIN (TP)*" if win and pnl > 0 else ("🛑 *STOP LOSS*" if pnl < 0 else "⚖️ *NEUTRAL*")
    pnl_sign    = "+" if win else "\\-"
    roi         = (pnl / (buy_price * shares) * 100) if (buy_price * shares > 0) else 0
    balance     = db.get_paper_balance(AGENT_USER_ID)
    return (
        f"🤖 *AUTO SELL — {result_text}*\n\n"
        f"📊 *Market:* BTC {_esc(_et_label(slug))} \\| *Dir:* *{_esc(outcome.upper())}*\n"
        f"💰 *Price:* `${_esc_code(f'{buy_price:.4f}')}` → `${_esc_code(f'{sell_price:.4f}')}`\n"
        f"💵 *PnL:* `{pnl_sign}${_esc_code(f'{abs(float(pnl)):.2f}')}` \\({_esc_code(f'{pnl_sign}{abs(float(roi)):.1f}%')} ROI\\)\n"
        f"₿ *BTC:* `{_esc_code(f'${btc_entry:,.0f}')}` → `{_esc_code(f'${btc_exit:,.0f}')}`\n"
        f"🏆 *Session:* `{_s.session_wins}W` / `{_s.session_losses}L` \\| Bal: `${_esc_code(f'{balance:.2f}')}`\n"
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
    """
    try:
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
            logger.warning(
                "[AGENT] Recovered orphaned position from DB: %s %s @ %.4f",
                p["outcome"], p["market_slug"], p["avg_price"]
            )
    except Exception as exc:
        logger.error("[AGENT] Orphan recovery error: %s", exc)


# ─── Auto-Sell & Exec ──────────────────────────────────────────────────────────

async def _attempt_sell(bot, slug: str, outcome: str,
                         buy_price: float, shares: float,
                         btc_entry: float, entry_ist: str,
                         retry: int = 0) -> bool:
    logger.info("[AGENT] Sell target: %s in %s (attempt %d)", outcome, slug, retry + 1)
    ok, msg = await _paper_sell_core(AGENT_USER_ID, outcome, None, slug)
    msg_str = str(msg).replace("\\", "")

    if ok:
        sell_price = buy_price
        m = re.search(r"@\s*\$([0-9.]+)", msg_str)
        if m: sell_price = float(m.group(1))
        
        pnl = (sell_price - buy_price) * shares
        if pnl >= 0: _s.session_wins += 1; _s.consecutive_losses = 0
        else: _s.session_losses += 1; _s.consecutive_losses += 1
        
        _s.session_pnl += pnl
        _s.current_slug = None; _s.current_outcome = None
        await _send(bot, _fmt_sell_alert(outcome, sell_price, buy_price, shares, pnl, slug, btc_entry, _btc_now(), entry_ist))
        return True

    if "awaiting" in msg_str.lower(): return False
    return True # stop retrying on hard error

async def _cycle(bot) -> None:
    if not _s.enabled: return
    if time.time() < _s.pause_until: return

    # Daily check
    today = _now_utc().date()
    balance = float(db.get_paper_balance(AGENT_USER_ID))
    if _s.last_day != today:
        _s.daily_start_balance = balance; _s.last_day = today
    if _s.daily_start_balance and (balance - _s.daily_start_balance) / _s.daily_start_balance <= -MAX_DAILY_LOSS_PCT:
        return

    # ── Already holding ────────────────────────────────────────────────────────
    if _s.current_slug and _s.current_outcome:
        # Check Scap Exits (HFT logic)
        async with aiohttp.ClientSession() as session:
            try:
                # Direct lookup of current price to avoid Gamma API lag
                from handlers import GAMMA_API_URL as GURL # type: ignore
                async with session.get(GURL.format(slug=str(_s.current_slug))) as r:
                    if r.status == 200:
                        data = await r.json()
                        markets = data[0].get("markets", []) if data else []
                        if markets:
                            outs = json.loads(markets[0].get("outcomes", "[]"))
                            tids = json.loads(markets[0].get("clobTokenIds", "[]"))
                            idx = -1
                            for i, o in enumerate(outs):
                                if o.lower() == str(_s.current_outcome).lower(): idx = i; break
                            if idx >= 0 and tids:
                                cp = await _get_clob_price(session, tids[idx], "buy")
                                if cp:
                                    roi = (cp - _s.current_buy_price) / _s.current_buy_price
                                    # Take Profit / Stop Loss
                                    if roi >= TAKE_PROFIT_PCT or roi <= -STOP_LOSS_PCT:
                                        logger.info("[AGENT] HFT EXIT: ROI=%.1f%% in %s", roi*100, _s.current_slug)
                                        await _attempt_sell(bot, str(_s.current_slug), str(_s.current_outcome), _s.current_buy_price, _s.current_shares, _s.current_btc_at_entry, _s.current_entry_ist)
                                        return
                                    # Signal Reversal
                                    if REVERSAL_EXIT:
                                        mp, _, _ = _compute_signal()
                                        if mp:
                                            is_up = str(_s.current_outcome).lower() == "up"
                                            if (is_up and mp < 0.45) or (not is_up and mp > 0.55):
                                                logger.info("[AGENT] REVERSAL EXIT: mp=%.3f", mp)
                                                await _attempt_sell(bot, str(_s.current_slug), str(_s.current_outcome), _s.current_buy_price, _s.current_shares, _s.current_btc_at_entry, _s.current_entry_ist)
                                                return
            except Exception: pass

        # Resolution check
        m, _ = await _get_target_market()
        if m and m.get("slug") != _s.current_slug:
            await _attempt_sell(bot, str(_s.current_slug), str(_s.current_outcome), _s.current_buy_price, _s.current_shares, _s.current_btc_at_entry, _s.current_entry_ist)
        return

    # ── Signal Eval ────────────────────────────────────────────────────────────
    market, _ = await _get_target_market()
    if not market: return
    slug = str(market.get("slug", ""))
    age  = _window_age_secs(slug)
    if age is None or age < 30 or age > 250: return

    m_prob, e_val, _ = _compute_signal()
    if m_prob is None or e_val is None: return
    m_p, edge = float(m_prob), float(e_val)
    if edge < AGENT_MIN_EDGE: return

    # Momentum metrics for the alert
    p1v = _price_n_secs_ago(60)
    p3v = _price_n_secs_ago(180)
    now_p = _btc_now()
    m1 = (now_p - p1v) / p1v if p1v else 0
    m3 = (now_p - p3v) / p3v if p3v else 0

    # Kelly Sizing
    t_amt = AGENT_TRADE_USD
    if balance > STARTING_BALANCE:
        t_amt = min(AGENT_TRADE_USD * 3, balance * KELLY_FRACTION)

    outcome = "Up" if m_p > 0.5 else "Down"
    ok, msg, _ = await _paper_buy_core(AGENT_USER_ID, outcome, t_amt, slug)
    if ok:
        _s.current_slug       = slug
        _s.current_outcome    = outcome
        # Parse fill price
        fill = 0.5
        mf = re.search(r"price.*?\$([0-9.]+)", str(msg).lower())
        if mf: 
            try: fill = float(mf.group(1))
            except Exception: pass
        _s.current_buy_price  = fill
        _s.current_shares     = t_amt / fill
        _s.current_btc_at_entry = now_p
        _s.current_entry_ist    = _now_ist_str()
        await _send(bot, _fmt_buy_alert(outcome, fill, edge, m_p, slug, m1, m3, t_amt))

# ─── Public API ────────────────────────────────────────────────────────────────

def toggle() -> bool:
    _s.enabled = not _s.enabled
    logger.info("[AGENT] Bot %s", "ENABLED" if _s.enabled else "PAUSED")
    return _s.enabled

def is_running() -> bool: return _s.running

async def start(bot):
    if _s.running: return
    _s.running = True
    _recover_orphaned_position()
    loop = asyncio.get_running_loop()
    _s.tasks.append(loop.create_task(_binance_ws_loop()))
    
    async def _main_loop():
        await _send(bot, _fmt_startup(db.get_paper_balance(AGENT_USER_ID)))
        while _s.running:
            await _cycle(bot)
            await asyncio.sleep(AGENT_POLL_SECONDS)
    
    _s.tasks.append(loop.create_task(_main_loop()))
    logger.info("[AGENT] Started v4 (HFT Mode)")

def stop():
    _s.running = False
    for t in _s.tasks: t.cancel()
    _s.tasks = []
    logger.info("[AGENT] Stopped")

def get_status_message() -> str:
    balance = db.get_paper_balance(AGENT_USER_ID)
    pnl = balance - STARTING_BALANCE
    psign = "+" if pnl >= 0 else ""
    return (
        f"🤖 *PolyAgent v4 — Status*\n\n"
        f"{'🟢 ACTIVE' if _s.enabled else '🔴 PAUSED'}\n"
        f"📡 *BTC:* `{_esc_code(_btc_now_str())}`\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}` \\({psign}${_esc_code(f'{pnl:.2f}')}\\)\n"
        f"🏆 *Session:* `{_s.session_wins}W` / `{_s.session_losses}L`\n"
        f"📝 *Last:* {_esc(str(_s.last_action)[:50])}\n"
        f"⚙️ *HFT:* Polling 1s \\| TP 8% \\| SL 5%\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )
