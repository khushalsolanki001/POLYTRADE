"""
api.py — Polymarket API client for PolyTrack Bot
=================================================
All HTTP calls to Polymarket's public APIs go through this module.
Uses aiohttp for non-blocking I/O so the bot event loop never stalls.

Confirmed API response shape (from live data-api.polymarket.com):
  {
    "proxyWallet":    "0x...",
    "side":           "BUY" | "SELL",
    "asset":          "...",
    "conditionId":    "0x...",
    "size":           1.5,           ← float, shares
    "price":          0.47,          ← float, USD per share
    "timestamp":      1740400000,    ← int, Unix seconds ✅
    "title":          "Will X...",   ← market title already included!
    "slug":           "will-x-...",
    "outcome":        "Yes" | "No" | "Up" | "Down"  ← human label
    "outcomeIndex":   0 | 1,
    ...
  }
"""
# it was for api 
import logging
import asyncio
from typing import Optional, Any
import aiohttp

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
TRADES_BASE_URL  = "https://data-api.polymarket.com/trades"
MARKETS_BASE_URL = "https://gamma-api.polymarket.com/markets"

# Shared client session (created once, reused)
_session: Optional[aiohttp.ClientSession] = None

# In-memory cache for market titles looked up via Gamma API
_market_cache: dict[str, str] = {}

_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ─────────────────────────────────────────────────────────────────────────────
#  Session lifecycle 123
# ─────────────────────────────────────────────────────────────────────────────

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=_TIMEOUT,
            headers={"User-Agent": "PolyTrackBot/1.0"},
        )
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
        logger.info("HTTP session closed.")


# ─────────────────────────────────────────────────────────────────────────────
#  Trade fetching
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_trades(wallet: str, limit: int = 20) -> list[dict[str, Any]]:
    """
    Fetch recent trades for *wallet* from Polymarket Data API.
    Returns a list sorted newest-first.  Returns [] on any error.
    """
    params = {
        "user":          wallet.lower(),
        "limit":         str(limit),
        "sortBy":        "TIMESTAMP",
        "sortDirection": "DESC",
    }
    session = await get_session()
    try:
        async with session.get(TRADES_BASE_URL, params=params) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("data", data.get("trades", []))
                return []
            logger.warning("Polymarket API %s for wallet %s", resp.status, wallet[:10])
            return []
    except asyncio.TimeoutError:
        logger.warning("Timeout fetching trades for %s", wallet[:10])
        return []
    except aiohttp.ClientError as exc:
        logger.error("HTTP error for %s: %s", wallet[:10], exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Market title — from trade itself first, Gamma API as fallback
# ─────────────────────────────────────────────────────────────────────────────

def get_trade_title(trade: dict) -> Optional[str]:
    """
    Extract market title directly from the trade dict.
    The Polymarket Data API already includes 'title' in every trade row,
    so no extra HTTP call is needed in most cases.
    """
    return (
        trade.get("title")
        or trade.get("question")
        or trade.get("name")
        or None
    )


async def fetch_market_title(market_id: str) -> Optional[str]:
    """
    Fallback: look up a title from the Gamma API when the trade lacks one.
    Results are cached in memory.
    """
    if not market_id:
        return None
    if market_id in _market_cache:
        return _market_cache[market_id]

    session = await get_session()
    try:
        async with session.get(f"{MARKETS_BASE_URL}/{market_id}") as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if isinstance(data, list) and data:
                    data = data[0]
                title = (
                    data.get("question")
                    or data.get("title")
                    or data.get("name")
                )
                if title:
                    _market_cache[market_id] = str(title)
                    return str(title)
    except Exception as exc:
        logger.debug("Gamma API lookup failed for %s: %s", market_id, exc)

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Trade field parsers — validated against live API response
# ─────────────────────────────────────────────────────────────────────────────

def parse_trade_type(trade: dict) -> str:
    """Return 'BUY' or 'SELL'. API uses 'side' field."""
    raw = str(trade.get("side") or trade.get("type") or trade.get("tradeType") or "").upper()
    if "BUY" in raw:
        return "BUY"
    if "SELL" in raw:
        return "SELL"
    return raw or "?"


def parse_trade_size(trade: dict) -> float:
    """Shares traded as float."""
    try:
        return float(trade.get("size") or trade.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_trade_price(trade: dict) -> float:
    """Price per share (0–1 USD)."""
    try:
        return float(trade.get("price") or trade.get("outcomePrice") or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_trade_usd_value(trade: dict) -> float:
    """USD value of trade: size × price (no special field in this API)."""
    if "usdcSize" in trade:
        try:
            return float(trade["usdcSize"])
        except (TypeError, ValueError):
            pass
    return parse_trade_size(trade) * parse_trade_price(trade)


def parse_trade_outcome(trade: dict) -> str:
    """
    Return the human label for which outcome was traded
    (e.g. 'Yes', 'No', 'Up', 'Down').
    The 'outcome' field contains this; do NOT fall back to 'side' here.
    """
    val = trade.get("outcome") or ""
    return str(val).strip()


def parse_trade_timestamp(trade: dict) -> int:
    """
    Return Unix epoch in seconds.
    The live API returns 'timestamp' as an integer (seconds).
    Handles the case where it comes back as a float or string too.
    """
    raw = trade.get("timestamp") or trade.get("createdAt") or trade.get("time") or 0
    try:
        ts = int(float(str(raw)))        # handles int, float, "1234567890"
        # Convert milliseconds to seconds if needed
        if ts > 9_999_999_999:
            ts = ts // 1000
        return ts
    except (TypeError, ValueError):
        return 0


def parse_market_id(trade: dict) -> str:
    """Return the conditionId or slug for secondary title lookup."""
    return str(
        trade.get("conditionId")
        or trade.get("market")
        or trade.get("slug")
        or trade.get("marketSlug")
        or ""
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Crypto Price Markets
# ─────────────────────────────────────────────────────────────────────────────

async def get_crypto_price_markets() -> list[dict[str, Any]]:
    """
    Fetch active 5-min and 15-min BTC/ETH/SOL markets from Gamma API.
    Used by PolyProfitBot to identify current trading cycles.
    """
    # Search for markets matching 'Bitcoin Price', 'Ethereum Price', 'Solana Price'
    # and look for 5/15 minute timeframes in the title.
    url = f"{MARKETS_BASE_URL}?active=true&limit=100"
    session = await get_session()
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                markets = await resp.json()
                crypto_markets = []
                for m in markets:
                    title = m.get("question", "").lower()
                    if any(coin in title for coin in ["bitcoin", "ethereum", "solana"]) and \
                       ("5-minute" in title or "15-minute" in title or "5 minute" in title or "15 minute" in title):
                        crypto_markets.append(m)
                return crypto_markets
            return []
    except Exception as e:
        logger.error(f"Error fetching crypto markets: {e}")
        return []

async def get_market_tokens(condition_id: str) -> dict[str, str]:
    """
    Fetch token IDs (asset_ids) for the YES and NO tokens of a given market.
    Returns a dict: {"YES": "id1", "NO": "id2"}
    """
    session = await get_session()
    try:
        async with session.get(f"{MARKETS_BASE_URL}/{condition_id}") as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list) and data:
                    data = data[0]
                
                tokens = {}
                for t in data.get("tokens", []):
                    outcome = t.get("outcome", "").upper()
                    if outcome in ["YES", "UP"]:
                        tokens["YES"] = t.get("token_id")
                    elif outcome in ["NO", "DOWN"]:
                        tokens["NO"] = t.get("token_id")
                return tokens
            return {}
    except Exception as e:
        logger.error(f"Error fetching tokens for market {condition_id}: {e}")
        return {}
