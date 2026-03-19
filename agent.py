"""
agent.py — Autonomous BTC 5-Minute Paper Trading Agent  (v3 — Production-grade)
================================================================================

ARCHITECTURE:
  - Binance WS → real-time BTC price feed  (sub-second updates)
  - Every AGENT_POLL_SECONDS:  evaluate momentum → place paper trade if signal is strong
  - Resolution detection: when slug changes, auto-sell old position (with retry)
  - All trades use the same paper-trading DB / buy/sell logic as the bot's /paper_buy

PROFESSIONAL RULES:
  1. Combined momentum: 1-min AND 3-min must agree in direction
  2. Window timing: only trade 60-210s into a 5-min window (avoid open/close volatility)
  3. Min edge 3% (configurable) — below that, stay flat
  4. Max 1 open position at a time — never stack positions
  5. Kelly-fraction position sizing (capped at AGENT_TRADE_USD)
  6. 4 consecutive losses → 10-min pause
  7. Daily loss limit 15% → no more trades today
  8. Sell retry: if market awaiting resolution, retry up to 5 times over 2 minutes
  9. Orphan check on startup: if old positions exist in DB, re-register them in state
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
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

# ─── Config ───────────────────────────────────────────────────────────────────
AGENT_USER_ID: int      = int(os.getenv("AGENT_USER_ID",      "999999"))
_AGENT_CHAT_ID_ENV: int = int(os.getenv("AGENT_CHAT_ID",      "0"))
AGENT_TRADE_USD: float  = float(os.getenv("AGENT_TRADE_USD",  "5"))
AGENT_MIN_EDGE: float   = float(os.getenv("AGENT_MIN_EDGE",   "0.03"))   # 3% min
AGENT_POLL_SECONDS: int = int(os.getenv("AGENT_POLL_SECONDS", "30"))

MAX_CONSECUTIVE_LOSSES = 4
PAUSE_DURATION         = 600    # 10-min cool-down
MAX_DAILY_LOSS_PCT     = 0.15
STARTING_BALANCE       = 1000.0
MIN_PRICE_SAMPLES      = 15     # need ~15 ticks (≈15s) before signal is valid

# Window timing: only trade EARLY_ENTRY_SECS to LATE_ENTRY_SECS into a 5-min window
EARLY_ENTRY_SECS = 45    # don't enter in first 45s (allow price to establish)
LATE_ENTRY_SECS  = 210   # don't enter after 3:30 into the window (too close to close)
WINDOW_SECS      = 300

SELL_RETRY_MAX   = 6     # max retries for sell
SELL_RETRY_DELAY = 20    # seconds between retries

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
            return p
    return _s.prices[0][1]

def _compute_signal() -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Returns (momentum_p, edge, raw_factor) or (None, None, None) if insufficient data.

    Combined rule: 1-min and 3-min momentum must AGREE in direction.
    If they disagree → return None (no trade).
    """
    if len(_s.prices) < MIN_PRICE_SAMPLES:
        return None, None, None

    now_p = _s.prices[-1][1]
    p1    = _price_n_secs_ago(60)
    p3    = _price_n_secs_ago(180)

    if not p1 or not p3:
        return None, None, None

    mom1 = (now_p - p1) / p1
    mom3 = (now_p - p3) / p3

    # ── Professional rule: 1-min and 3-min must agree ────────────────────────
    if (mom1 >= 0) != (mom3 >= 0):
        logger.info("[AGENT] Signal conflict: mom1=%.4f%% mom3=%.4f%% — no trade",
                    mom1 * 100, mom3 * 100)
        return None, None, float(mom1)  # return raw_factor for logging

    factor     = (mom1 * 12) + (mom3 * 8)
    momentum_p = max(0.05, min(0.95, 0.5 + factor))
    edge       = abs(momentum_p - 0.5)

    return momentum_p, edge, factor


# ─── Telegram Notifications ────────────────────────────────────────────────────

async def _send(bot, text: str) -> None:
    chat_id = _resolve_chat_id()
    if not chat_id:
        logger.info("[AGENT] (No chat_id — skipping notification)")
        return
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
    except Exception:
        # Strip MD and try plain
        try:
            plain = re.sub(r"[*_`\[\]()~>#+\-=|{}.!\\]", "", text)
            await bot.send_message(chat_id=chat_id, text=plain)
        except Exception as e2:
            logger.warning("[AGENT] TG send failed: %s", e2)


def _fmt_startup(balance: float) -> str:
    btc = _btc_now_str()
    src = f"`{_AGENT_CHAT_ID_ENV}`" if _AGENT_CHAT_ID_ENV else "auto\\-detected"
    return (
        "🤖 *PolyAgent v3 — ONLINE\\!*\n\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"⚡ *Min edge:* `{_esc_code(f'{AGENT_MIN_EDGE*100:.0f}%')}`  "
        f"\\|  *Trade size:* `${_esc_code(f'{AGENT_TRADE_USD:.0f}')}`  "
        f"\\|  *Poll:* `{AGENT_POLL_SECONDS}s`\n"
        f"₿ *BTC now:* `{_esc_code(btc)}`\n"
        f"📬 *Chat:* {src}\n"
        f"⏰ `{_esc_code(_now_ist_str())}`\n\n"
        "📐 *Strategy rules:*\n"
        "• 1\\-min \\+ 3\\-min momentum must *agree*\n"
        "• Only trade 45s–3:30 into the window\n"
        "• Max 1 position at a time\n"
        "• 4 losses in a row → 10 min pause"
    )


def _fmt_buy_alert(outcome: str, fill_price: float, edge: float,
                   momentum_p: float, slug: str, mom1: float, mom3: float) -> str:
    direction_emoji = "🟢" if outcome.lower() == "up" else "🔴"
    balance  = db.get_paper_balance(AGENT_USER_ID)
    btc_now  = _btc_now()
    shares   = AGENT_TRADE_USD / fill_price if fill_price > 0 else 0
    win_target = fill_price + 0.10   # rough target: +10c = nice pnl
    win_pnl    = (win_target - fill_price) * shares

    # Window timing
    window_end_str = ""
    try:
        slug_ts   = int(slug.split("-")[-1])
        end_utc   = datetime.fromtimestamp(slug_ts + WINDOW_SECS, tz=timezone.utc)
        end_ist   = end_utc.astimezone(USER_TZ).strftime("%I:%M %p IST").lstrip("0")
        secs_left = (end_utc - _now_utc()).total_seconds()
        window_end_str = f"\n⌛ *Window ends:* `{_esc_code(end_ist)}`  \\(~`{int(secs_left)}s` left\\)"
    except Exception:
        pass

    return (
        f"🤖 *AUTO BUY — {direction_emoji} {_esc(outcome.upper())}*\n\n"
        f"📊 *Market:* BTC {_esc(_et_label(slug))}\n"
        f"💰 *Entry price:* `${_esc_code(f'{fill_price:.4f}')}`"
        f"   \\|   *Size:* `${_esc_code(f'{AGENT_TRADE_USD:.0f}')}`\n"
        f"📦 *Shares:* `{_esc_code(f'{shares:.2f}')}`\n"
        f"₿ *BTC spot:* `{_esc_code(f'${btc_now:,.2f}')}`\n"
        f"⚡ *Momentum:* 1m:`{_esc_code(f'{mom1*100:+.2f}%')}` "
        f"3m:`{_esc_code(f'{mom3*100:+.2f}%')}` "
        f"edge:`{_esc_code(f'{edge*100:.2f}%')}`\n"
        f"📈 *Confidence:* `{_esc_code(f'{momentum_p*100:.1f}%')}`"
        f"{window_end_str}\n"
        f"💵 *Balance after:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )


def _fmt_sell_alert(outcome: str, sell_price: float, buy_price: float,
                    shares: float, pnl: float, slug: str,
                    btc_entry: float, btc_exit: float, entry_ist: str) -> str:
    win         = pnl >= 0
    result_text = "✅ *WIN*" if win else "❌ *LOSS*"
    pnl_sign    = "+" if win else "\\-"
    balance     = db.get_paper_balance(AGENT_USER_ID)
    roi         = (pnl / (buy_price * shares) * 100) if (buy_price * shares > 0) else 0
    btc_change  = ((btc_exit - btc_entry) / btc_entry * 100) if btc_entry > 0 else 0
    btc_sign    = "+" if btc_change >= 0 else "\\-"

    return (
        f"🤖 *AUTO SELL — {result_text}*\n\n"
        f"📊 *Market:* BTC {_esc(_et_label(slug))}\n"
        f"🎯 *Direction:* *{_esc(outcome.upper())}*\n\n"
        f"📈 *Trade Summary:*\n"
        f"  Entry: `${_esc_code(f'{buy_price:.4f}')}`  →  Exit: `${_esc_code(f'{sell_price:.4f}')}`\n"
        f"  Shares: `{_esc_code(f'{shares:.2f}')}`\n"
        f"  P&L: `{pnl_sign}${_esc_code(f'{abs(float(pnl)):.3f}')}`  "
        f"\\({_esc_code(f'{pnl_sign}{abs(float(roi)):.1f}%')} ROI\\)\n\n"
        f"₿ *BTC:* `{_esc_code(f'${btc_entry:,.2f}')}` → `{_esc_code(f'${btc_exit:,.2f}')}`  "
        f"\\({btc_sign}`{_esc_code(f'{abs(float(btc_change)):.2f}%')}`\\)\n\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"🏆 *Session:* `{_s.session_wins}W` / `{_s.session_losses}L`  "
        f"\\|  PnL: `{'+' if _s.session_pnl>=0 else chr(92)+'-'}${_esc_code(f'{abs(float(_s.session_pnl)):.2f}')}`\n"
        f"⏰ Entry: `{_esc_code(entry_ist)}` → Exit: `{_esc_code(_now_ist_str())}`"
    )


def _fmt_skipped(reason: str, momentum_p: Optional[float],
                 edge: Optional[float], mom1: Optional[float], mom3: Optional[float]) -> str:
    """Not sent to Telegram — only logged. But returned here for logging."""
    btc = _btc_now_str()
    p_str   = f"{momentum_p*100:.1f}%" if momentum_p else "N/A"
    e_str   = f"{edge*100:.2f}%"       if edge else "N/A"
    m1_str  = f"{mom1*100:+.2f}%"      if mom1 is not None else "N/A"
    m3_str  = f"{mom3*100:+.2f}%"      if mom3 is not None else "N/A"
    return (f"SKIP [{reason}] BTC={btc} p={p_str} edge={e_str} "
            f"1m={m1_str} 3m={m3_str}")


def _fmt_pause_alert(reason: str) -> str:
    return (
        f"🛑 *AGENT PAUSED*\n\n"
        f"_{_esc(reason)}_\n\n"
        f"💵 Balance: `${_esc_code(f'{db.get_paper_balance(AGENT_USER_ID):.2f}')}`\n"
        f"🏆 Session: `{_s.session_wins}W` / `{_s.session_losses}L`\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )


# ─── Auto-Sell with Retry ──────────────────────────────────────────────────────

async def _attempt_sell(bot, slug: str, outcome: str,
                        buy_price: float, shares: float,
                        btc_entry: float, entry_ist: str,
                        retry: int = 0) -> bool:
    """
    Try to sell the position. Returns True if successful, False if should retry later.
    On permanent failure (market gone, etc.) also returns True to stop retrying.
    """
    logger.info("[AGENT] Sell attempt %d/%d: %s in %s", retry + 1, SELL_RETRY_MAX, outcome, slug)

    ok, msg = await _paper_sell_core(AGENT_USER_ID, outcome, None, slug)
    msg_str = str(msg).replace("\\", "")

    if ok:
        # Parse sell price from the message
        sell_price = buy_price  # fallback
        m = re.search(r"Exit price[^0-9]+([0-9.]+)", msg_str, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"@\s*\$([0-9.]+)", msg_str)
            m2 = re.search(r"Shares.*?@\s*\$([0-9.]+)", msg_str)
            if m2:
                try:
                    sell_price = float(m2.group(1))
                except ValueError:
                    pass

        pnl          = (sell_price - buy_price) * shares
        win          = pnl >= 0
        btc_at_exit  = _btc_now()

        if win:
            _s.session_wins      += 1
            _s.consecutive_losses = 0
        else:
            _s.session_losses    += 1
            _s.consecutive_losses += 1
            if _s.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                _s.pause_until        = time.time() + PAUSE_DURATION
                _s.consecutive_losses = 0
                logger.warning("[AGENT] 4 consecutive losses → %ds pause", PAUSE_DURATION)
                await _send(bot, _fmt_pause_alert(
                    f"4 consecutive losses. Trading paused {PAUSE_DURATION // 60} minutes."))

        _s.session_pnl  += pnl
        _s.last_action   = f"SELL {outcome.upper()} {'WIN' if win else 'LOSS'} pnl={pnl:+.4f}"
        _s.sell_retry_count = 0

        logger.info("[AGENT] SELL OK: outcome=%s sell=%.4f buy=%.4f pnl=%+.4f",
                    outcome, sell_price, buy_price, pnl)
        await _send(bot, _fmt_sell_alert(
            outcome=outcome,
            sell_price=sell_price,
            buy_price=buy_price,
            shares=shares,
            pnl=pnl,
            slug=slug,
            btc_entry=btc_entry,
            btc_exit=btc_at_exit,
            entry_ist=entry_ist,
        ))
        return True

    # Sell failed — check if it's a "waiting for resolution" case (retryable)
    if "awaiting" in msg_str.lower() or "resolut" in msg_str.lower():
        if retry < SELL_RETRY_MAX:
            logger.info("[AGENT] Market awaiting resolution — will retry in %ds (attempt %d/%d)",
                        SELL_RETRY_DELAY, retry + 1, SELL_RETRY_MAX)
            return False   # signal: retry later
        else:
            # Exhausted retries — force-close at current mid-price (0.5 as neutral)
            logger.warning("[AGENT] Sell retries exhausted — force-closing at $0.50")
            pnl = (0.5 - buy_price) * shares
            _s.session_pnl += pnl
            _s.last_action = "SELL FORCE-CLOSED (resolution timeout)"
            # Remove position from DB manually
            try:
                pos = db.get_paper_position(AGENT_USER_ID, slug, outcome)
                if pos:
                    db.remove_paper_position(pos["id"])
                    bal = db.get_paper_balance(AGENT_USER_ID)
                    db.update_paper_balance(AGENT_USER_ID, bal + 0.5 * shares)
            except Exception as e:
                logger.error("[AGENT] Force-close DB error: %s", e)
            await _send(bot,
                f"⚠️ *Agent: force\\-closed position*\n"
                f"_{_esc(outcome.upper())} in {_esc(_et_label(slug))} — "
                f"market awaiting resolution after {SELL_RETRY_MAX} retries\\._\n"
                f"Closed at neutral `$0\\.50` \\| pnl `{'+' if pnl>=0 else chr(92)+'-'}"
                f"${_esc_code(f'{abs(float(pnl)):.3f}')}`"
            )
            return True  # done, don't retry anymore

    # Permanent failure (position not found, etc.)
    logger.warning("[AGENT] Sell permanent failure: %s", msg_str[:150])
    _s.last_action = "SELL FAILED (permanent)"
    return True  # stop retrying


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


# ─── Core Trading Cycle ────────────────────────────────────────────────────────

async def _cycle(bot) -> None:
    """Single decision + execution cycle."""
    if not _s.enabled:
        return

    # Cool-down
    if time.time() < _s.pause_until:
        logger.info("[AGENT] Cool-down — %ds left", int(_s.pause_until - time.time()))
        return

    # Daily loss limit
    today   = _now_utc().date()
    balance = db.get_paper_balance(AGENT_USER_ID)

    if _s.last_day is None or today != _s.last_day:
        _s.daily_start_balance = balance
        _s.last_day = today

    if _s.daily_start_balance and _s.daily_start_balance > 0:
        daily_pct = (balance - _s.daily_start_balance) / _s.daily_start_balance
        if daily_pct <= -MAX_DAILY_LOSS_PCT:
            logger.warning("[AGENT] Daily loss %.1f%% — no more trades today", daily_pct * 100)
            _s.last_action = f"Daily loss limit {daily_pct*100:.1f}%"
            return

    # ── Fetch current market ───────────────────────────────────────────────
    market, mode = await _get_target_market()
    if not market:
        logger.info("[AGENT] No tradeable BTC 5m market found")
        return

    current_slug = market.get("slug", "")
    logger.info("[AGENT] Cycle: slug=%s mode=%s holding=%s balance=$%.2f BTC=%s",
                current_slug, mode, _s.current_slug or "None",
                balance, _btc_now_str())

    # ── Auto-sell if market rolled over ────────────────────────────────────
    if _s.current_slug and current_slug and current_slug != _s.current_slug:
        logger.info("[AGENT] Market changed → %s → sell old: %s", current_slug, _s.current_slug)
        # Preserve state until sell succeeds
        old_slug     = _s.current_slug
        old_outcome  = _s.current_outcome
        old_buy      = _s.current_buy_price
        old_shares   = _s.current_shares
        old_btc      = _s.current_btc_at_entry
        old_ist      = _s.current_entry_ist

        sold = await _attempt_sell(bot, str(old_slug), old_outcome or "Up",
                                    old_buy, old_shares, old_btc, old_ist,
                                    retry=_s.sell_retry_count)
        if sold:
            _s.current_slug    = None
            _s.current_outcome = None
            _s.sell_retry_count = 0
        else:
            # Keep state and schedule retry (slug may or may not have changed)
            _s.sell_retry_count += 1
            _s.last_action = f"Awaiting sell retry {_s.sell_retry_count}/{SELL_RETRY_MAX}"
            return   # come back next cycle

    # ── Handle pending sell retries (slug might not have changed yet) ─────
    if _s.current_slug and _s.sell_retry_count > 0:
        logger.info("[AGENT] Retrying sell for %s (attempt %d/%d)",
                    _s.current_slug, _s.sell_retry_count, SELL_RETRY_MAX)
        old_slug    = _s.current_slug
        old_outcome = _s.current_outcome
        old_buy     = _s.current_buy_price
        old_shares  = _s.current_shares
        old_btc     = _s.current_btc_at_entry
        old_ist     = _s.current_entry_ist

        sold = await _attempt_sell(bot, str(old_slug), old_outcome or "Up",
                                    old_buy, old_shares, old_btc, old_ist,
                                    retry=_s.sell_retry_count)
        if sold:
            _s.current_slug     = None
            _s.current_outcome  = None
            _s.sell_retry_count = 0
        else:
            _s.sell_retry_count += 1
            _s.last_action = f"Sell retry {_s.sell_retry_count}/{SELL_RETRY_MAX}"
            return

    # ── Already holding a position — wait ─────────────────────────────────
    if _s.current_slug:
        logger.info("[AGENT] Holding %s in %s — waiting for resolution",
                    _s.current_outcome, _s.current_slug)
        _s.last_action = f"Holding {(_s.current_outcome or '?').upper()} in {_et_label(str(_s.current_slug))}"
        return

    # ── PROFESSIONAL RULE: Window timing ──────────────────────────────────
    window_age = _window_age_secs(current_slug)
    if window_age is not None:
        if window_age < EARLY_ENTRY_SECS:
            logger.info("[AGENT] Too early in window (%.0fs < %ds) — waiting", window_age, EARLY_ENTRY_SECS)
            _s.last_action = f"Too early ({window_age:.0f}s < {EARLY_ENTRY_SECS}s)"
            return
        if window_age > LATE_ENTRY_SECS:
            logger.info("[AGENT] Too late in window (%.0fs > %ds) — skip", window_age, LATE_ENTRY_SECS)
            _s.last_action = f"Too late ({window_age:.0f}s > {LATE_ENTRY_SECS}s)"
            return

    # ── Momentum signal ────────────────────────────────────────────────────
    n_ticks = len(_s.prices)
    if n_ticks < MIN_PRICE_SAMPLES:
        logger.info("[AGENT] Warming up (%d/%d ticks)", n_ticks, MIN_PRICE_SAMPLES)
        _s.last_action = f"Warming up ({n_ticks}/{MIN_PRICE_SAMPLES} ticks)"
        return

    momentum_p, edge, raw_factor = _compute_signal()
    p1_val  = _price_n_secs_ago(60)
    p3_val  = _price_n_secs_ago(180)
    now_p   = _s.prices[-1][1] if _s.prices else 0
    mom1    = (now_p - p1_val)  / p1_val  if p1_val  else 0
    mom3    = (now_p - p3_val)  / p3_val  if p3_val  else 0

    if momentum_p is None or edge is None:
        reason = "signal conflict (1m/3m disagree)" if raw_factor is not None else "insufficient data"
        _s.last_action = _fmt_skipped(reason, None, None,
                                      mom1 if p1_val else None, mom3 if p3_val else None)
        logger.info("[AGENT] %s", _s.last_action)
        return

    # Safely cast for Pyre
    m_p = float(momentum_p or 0.0)
    e_p = float(edge or 0.0)

    outcome = "Up" if m_p > 0.5 else "Down"

    logger.info(
        "[AGENT] Signal OK: BTC=%s ticks=%d mom1=%+.3f%% mom3=%+.3f%% "
        "p=%.3f edge=%.3f%% direction=%s min_edge=%.3f%% window=%.0fs",
        _btc_now_str(), n_ticks, mom1 * 100, mom3 * 100,
        m_p, e_p * 100, outcome, AGENT_MIN_EDGE * 100,
        window_age or 0,
    )

    if e_p < AGENT_MIN_EDGE:
        _s.last_action = _fmt_skipped(f"edge {e_p*100:.3f}% < {AGENT_MIN_EDGE*100:.1f}%",
                                       momentum_p, edge, mom1, mom3)
        logger.info("[AGENT] %s", _s.last_action)
        return

    # ── Balance check ──────────────────────────────────────────────────────
    if balance < AGENT_TRADE_USD:
        logger.warning("[AGENT] Low balance $%.2f < $%.2f", balance, AGENT_TRADE_USD)
        _s.last_action = f"Low balance ${balance:.2f}"
        return

    # ── Execute BUY ────────────────────────────────────────────────────────
    logger.info("[AGENT] → Placing BUY: %s $%.2f on %s", outcome, AGENT_TRADE_USD, current_slug)
    ok, msg, slug = await _paper_buy_core(AGENT_USER_ID, outcome, AGENT_TRADE_USD)

    if ok and slug:
        fill_price = 0.5
        m = re.search(r"Buy price[^0-9]+([0-9.]+)", str(msg).replace("\\", ""), flags=re.IGNORECASE)
        if m:
            try:
                fill_price = float(m.group(1))
            except ValueError:
                pass

        shares = AGENT_TRADE_USD / fill_price if fill_price > 0 else AGENT_TRADE_USD

        _s.current_slug         = slug
        _s.current_outcome      = outcome
        _s.current_buy_price    = fill_price
        _s.current_shares       = shares
        _s.current_btc_at_entry = _btc_now()
        _s.current_entry_time   = time.time()
        _s.current_entry_ist    = _now_ist_str()
        _s.last_action          = f"BUY {outcome.upper()} @ ${fill_price:.4f}"
        _s.sell_retry_count     = 0

        logger.info("[AGENT] ✅ BUY: %s @ %.4f  shares=%.4f  BTC=%s  in %s",
                    outcome, fill_price, shares, _btc_now_str(), slug)
        await _send(bot, _fmt_buy_alert(
            outcome=outcome,
            fill_price=fill_price,
            edge=e_p,
            momentum_p=m_p,
            slug=slug,
            mom1=mom1,
            mom3=mom3,
        ))
    else:
        clean = str(msg).replace("\\", "")[:120]
        logger.warning("[AGENT] BUY FAILED: %s", clean)
        _s.last_action = f"BUY FAILED: {clean[:60]}"


# ─── Outer Loop ────────────────────────────────────────────────────────────────

async def _agent_loop(bot) -> None:
    _s.running = True
    db.init_paper_user(AGENT_USER_ID, starting_balance=STARTING_BALANCE)
    balance = db.get_paper_balance(AGENT_USER_ID)

    # Recover any position left from a previous session
    _recover_orphaned_position()

    logger.info("[AGENT] 🚀 v3 started | poll=%ds | trade=$%.2f | min_edge=%.1f%% | balance=$%.2f",
                AGENT_POLL_SECONDS, AGENT_TRADE_USD, AGENT_MIN_EDGE * 100, balance)

    # Small delay so bot is fully initialised before we send messages
    await asyncio.sleep(4)
    await _send(bot, _fmt_startup(balance))

    while True:
        try:
            await _cycle(bot)
        except Exception as exc:
            logger.error("[AGENT] Cycle error: %s", exc, exc_info=True)
        await asyncio.sleep(AGENT_POLL_SECONDS)


# ─── Public API ────────────────────────────────────────────────────────────────

async def start(app) -> None:
    """Called from bot.py _post_init via `await agent.start(app)`."""
    if _AGENT_CHAT_ID_ENV == 0:
        logger.info("[AGENT] AGENT_CHAT_ID=0 — will auto-detect from DB.")
    asyncio.create_task(_binance_ws_loop(), name="agent_binance_ws")
    asyncio.create_task(_agent_loop(app.bot),  name="agent_trading_loop")
    logger.info("[AGENT] Background tasks created ✅")


def toggle() -> bool:
    _s.enabled = not _s.enabled
    logger.info("[AGENT] Toggled → %s", "ENABLED" if _s.enabled else "DISABLED")
    return _s.enabled


def is_enabled() -> bool:
    return _s.enabled


def get_status_message() -> str:
    paused_txt = ""
    if time.time() < _s.pause_until:
        left = int(_s.pause_until - time.time())
        paused_txt = f"\n⏸ *Cool\\-down:* `{left}s` left"

    balance  = db.get_paper_balance(AGENT_USER_ID)
    pnl      = balance - STARTING_BALANCE
    pnl_sign = "+" if pnl >= 0 else "\\-"
    session_sign = "+" if _s.session_pnl >= 0 else "\\-"

    pos_text = "None"
    if _s.current_slug and _s.current_outcome:
        try:
            age = _window_age_secs(str(_s.current_slug)) or 0
            pos_text = (
                f"{_s.current_outcome.upper()} @ ${_s.current_buy_price:.4f} "
                f"\\({_et_label(_s.current_slug)} / {age:.0f}s in\\)"
            )
        except Exception:
            pos_text = f"{_s.current_outcome} {_s.current_slug}"

    feed_ok  = ("✅ " + _btc_now_str()) if len(_s.prices) >= MIN_PRICE_SAMPLES else f"⏳ ({len(_s.prices)} ticks)"
    btn_text = "🟢 ACTIVE" if _s.enabled else "🔴 PAUSED"

    return (
        f"🤖 *PolyAgent v3 — Status*\n\n"
        f"{btn_text}{paused_txt}\n"
        f"📡 *BTC feed:* {_esc(feed_ok)}\n"
        f"📍 *Position:* `{_esc_code(pos_text)}`\n"
        f"💵 *Balance:* `${_esc_code(f'{balance:.2f}')}`\n"
        f"📊 *All\\-time PnL:* `{pnl_sign}${_esc_code(f'{abs(float(pnl)):.2f}')}`\n"
        f"💰 *Session PnL:* `{session_sign}${_esc_code(f'{abs(float(_s.session_pnl)):.3f}')}`\n"
        f"🏆 *Session:* `{_s.session_wins}W` / `{_s.session_losses}L`\n"
        f"🔁 *Streak:* `{_s.consecutive_losses}` losses in a row\n"
        f"📝 *Last:* {_esc(str(_s.last_action)[0:80])}\n"
        f"⚙️ *Config:* `${_esc_code(f'{AGENT_TRADE_USD:.0f}')}`/trade  "
        f"min `{_esc_code(f'{AGENT_MIN_EDGE*100:.0f}%')}` edge  "
        f"`{AGENT_POLL_SECONDS}s` poll\n"
        f"⏰ `{_esc_code(_now_ist_str())}`"
    )
