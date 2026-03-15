import asyncio
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Set, Tuple

import numpy as np
import pandas as pd
import requests
from telegram.ext import Application
from xgboost import XGBClassifier

from ml_config import LiveConfig, LOG_DIR
from data_and_features import _extract_features_first4


def _setup_logger():
    import logging

    logger = logging.getLogger("ml_signals")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(LOG_DIR / "ml_signals.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


logger = _setup_logger()


_last_signalled_window: Optional[pd.Timestamp] = None


def _load_model(path: str) -> XGBClassifier:
    model = XGBClassifier()
    model.load_model(path)
    return model


def _fetch_recent_klines(limit: int = 10) -> pd.DataFrame:
    """
    Fetch recent 1m BTCUSDT klines from Binance REST.
    We keep this lightweight so it can run alongside the Telegram bot.
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1m", "limit": limit}
    r = requests.get(url, params=params, timeout=5)
    r.raise_for_status()
    data = r.json()

    rows = []
    for k in data:
        # kline format: [open_time, open, high, low, close, volume, close_time, ...]
        open_time = datetime.fromtimestamp(k[0] / 1000.0, tz=timezone.utc)
        rows.append(
            {
                "open_time": open_time,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
        )
    df = pd.DataFrame(rows).set_index("open_time").sort_index()
    return df


def _current_window_start(ts: pd.Timestamp, window_minutes: int) -> pd.Timestamp:
    minute = (ts.minute // window_minutes) * window_minutes
    return ts.replace(second=0, microsecond=0, minute=minute)


def _build_single_window_features(
    df_1m: pd.DataFrame, window_minutes: int
) -> Optional[Tuple[pd.Timestamp, Dict[str, float], float]]:
    """
    From a small recent-1m DataFrame, build features for the latest full
    5-minute window using only the first 4 candles.
    """
    if df_1m.empty:
        return None

    latest_ts = df_1m.index.max()
    window_start = _current_window_start(latest_ts, window_minutes)
    window_end = window_start + pd.Timedelta(minutes=window_minutes)

    mask = (df_1m.index >= window_start) & (df_1m.index < window_end)
    w = df_1m.loc[mask].sort_index()
    if len(w) < 5:
        return None

    first4 = w.iloc[:4].copy()
    start_price = float(first4["close"].iloc[0])
    feats = _extract_features_first4(first4)
    return window_start, feats, start_price


async def generate_single_signal_text() -> str:
    """
    Generate a one-off ML signal string using the latest completed 5-minute
    Binance window. This is used by a Telegram command so you can see what
    the model is thinking on-demand, even if the background loop is failing.
    """
    cfg = LiveConfig()
    if not cfg.model_path.exists():
        return "ML error: trained model file not found on disk."

    try:
        df_1m = _fetch_recent_klines(limit=10)
    except Exception as e:
        logger.error("Error fetching Binance klines for single signal: %s", e)
        return f"ML error: could not fetch Binance data: {e}"

    res = _build_single_window_features(df_1m, cfg.window_minutes)
    if res is None:
        return "ML info: not enough 1m candles yet for a full 5-minute window."

    window_start, feats, start_price = res

    try:
        model = _load_model(str(cfg.model_path))
    except Exception as e:
        logger.error("Error loading model for single signal: %s", e)
        return f"ML error: could not load model: {e}"

    feature_cols = sorted(feats.keys())
    x = np.array([[feats[c] for c in feature_cols]], dtype=float)
    prob_up = float(model.predict_proba(x)[0, 1])

    fair_price = 0.5
    edge = prob_up - fair_price
    direction = "UP" if prob_up >= 0.5 else "DOWN"

    ts_str = window_start.strftime("%Y-%m-%d %H:%M UTC")
    # For Polymarket-style reasoning, the 5-minute "target" is the
    # start snapshot price: the market resolves Up if final price
    # >= this level, Down otherwise.
    lines = [
        "BTC 5-minute ML snapshot",
        f"Window: {ts_str}",
        f"Target (5m start price): {start_price:,.2f} USDT",
        f"Model P(Up >= target): {prob_up:.3f}",
        f"Edge vs 50/50: {edge:.3f}",
        f"Direction: {direction} (vs target)",
    ]
    return "\n".join(lines)


async def run_ml_signal_loop(app: Application) -> None:
    """
    Background loop that, every ~30s, looks at the latest 5-minute BTC window,
    runs it through the trained model, and pushes a signal message to all
    tracked chats:

        - Window timestamp (5-minute bucket)
        - Model probability of Up
        - Direction (Up/Down) and a simple "buy YES" suggestion threshold

    This is intentionally simple and stateless: one signal per 5-minute window.
    """
    cfg = LiveConfig()
    if not cfg.model_path.exists():
        logger.warning("ML model file not found; signal loop disabled.")
        return

    model = _load_model(str(cfg.model_path))

    # Destination chats for signals:
    # read from env ML_SIGNAL_CHAT_IDS as comma-separated list of chat IDs.
    ids_raw = os.getenv("ML_SIGNAL_CHAT_IDS", "").strip()
    chat_ids: Set[int] = set()
    if ids_raw:
        for part in ids_raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                chat_ids.add(int(part))
            except ValueError:
                logger.warning("Invalid chat id in ML_SIGNAL_CHAT_IDS: %s", part)

    if not chat_ids:
        logger.warning(
            "No chat IDs configured for ML signals. "
            "Set ML_SIGNAL_CHAT_IDS in your .env to a Telegram chat id."
        )

    global _last_signalled_window

    logger.info("Starting ML signal loop for 5-minute BTC Up/Down…")

    while True:
        try:
            df_1m = _fetch_recent_klines(limit=10)
        except Exception as e:
            logger.error("Error fetching Binance klines: %s", e)
            await asyncio.sleep(cfg.polling_interval_sec)
            continue

        res = _build_single_window_features(df_1m, cfg.window_minutes)
        if res is None:
            await asyncio.sleep(cfg.polling_interval_sec)
            continue

        window_start, feats, start_price = res

        # Avoid sending multiple signals for the same 5-minute bucket
        if _last_signalled_window is not None and window_start <= _last_signalled_window:
            await asyncio.sleep(cfg.polling_interval_sec)
            continue

        _last_signalled_window = window_start

        # Align feature order by sorted keys (matches training usage)
        feature_cols = sorted(feats.keys())
        x = np.array([[feats[c] for c in feature_cols]], dtype=float)
        prob_up = float(model.predict_proba(x)[0, 1])

        # Simple internal fair price proxy (no external Polymarket dependency).
        # We treat a neutral 50/50 as the "market" and look for edge vs 0.5.
        fair_price = 0.5
        edge = prob_up - fair_price
        direction = "UP" if prob_up >= 0.5 else "DOWN"

        # BUY YES when model prob is high and clearly above 0.5 by edge_buffer.
        buy_yes = prob_up >= cfg.prob_threshold and edge >= cfg.edge_buffer

        ts_str = window_start.strftime("%Y-%m-%d %H:%M UTC")
        # In Polymarket 5m BTC Up/Down, the "target" is the start snapshot
        # level (e.g. 71,500). We approximate that with the Binance 1m
        # close at the start of the window.
        lines = [
            "BTC 5-minute ML signal",
            f"Window: {ts_str}",
            f"Target (5m start price): {start_price:,.2f} USDT",
            f"Model P(Up >= target): {prob_up:.3f}",
            f"Edge vs 50/50: {edge:.3f}",
            f"Direction: {direction} (vs target)",
        ]

        if buy_yes:
            lines.append("Decision: BUY YES (strong Up edge vs Polymarket price).")
        else:
            lines.append("Decision: No strong BUY YES edge (wait / manage positions).")

        msg = "\n".join(lines)

        # Broadcast to all tracked chats
        for chat_id in chat_ids:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning("Failed to send ML signal to chat %s: %s", chat_id, e)

        await asyncio.sleep(cfg.polling_interval_sec)

