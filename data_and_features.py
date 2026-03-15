import glob
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from ml_config import DataConfig, LOG_DIR


BINANCE_COLS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]


def _setup_logger():
    import logging

    logger = logging.getLogger("data_and_features")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(LOG_DIR / "data_and_features.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


logger = _setup_logger()


def _find_csvs(cfg: DataConfig) -> List[Path]:
    pattern = str(cfg.data_dir / f"{cfg.symbol_prefix}-*.csv")
    files = sorted(Path(p) for p in glob.glob(pattern))
    return files


def _load_binance_file(path: Path) -> pd.DataFrame:
    """
    Load a single Binance 1m kline CSV (no header), convert open_time (ms)
    to UTC datetime index, and ensure numeric OHLCV columns.
    """
    df = pd.read_csv(path, header=None, names=BINANCE_COLS)

    # Some files can contain corrupted timestamps (e.g. far future years)
    # which overflow pandas' nanosecond datetime range. We coerce those
    # to NaT and drop them so they don't poison the dataset.
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
    df = df.dropna(subset=["open_time"])
    df["open_time"] = df["open_time"].astype("int64")
    df["open_time"] = pd.to_datetime(
        df["open_time"], unit="ms", utc=True, errors="coerce"
    )
    df = df.dropna(subset=["open_time"])
    df.set_index("open_time", inplace=True)
    df.sort_index(inplace=True)

    num_cols = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_base",
        "taker_buy_quote",
    ]
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def _extract_features_first4(window: pd.DataFrame) -> Dict[str, float]:
    """
    Feature engineering for the first 4 candles of a 5-minute window.

    Rationale:
    - The strongest exploitable edge in 5-minute BTC Up/Down markets comes from
      continuation of early-window orderflow (minutes 1–4), which propagates
      into the 5th minute and into Polymarket with ~0.5–2s lag.
    - We intentionally ignore the 5th candle to avoid label leakage.
    """
    assert len(window) == 4

    o = window["open"].values
    h = window["high"].values
    l = window["low"].values
    c = window["close"].values
    v = window["volume"].values

    base = c[0]
    ret_1 = (c[1] / base) - 1.0
    ret_2 = (c[2] / base) - 1.0
    ret_3 = (c[3] / base) - 1.0

    feats: Dict[str, float] = {
        "mom_1": float(ret_1),
        "mom_2": float(ret_2),
        "mom_3": float(ret_3),
        "mom_4": float((c[3] / base) - 1.0),
    }

    # Directionality: up/down counts and streaks in first 4 candles
    dirs = np.sign(c - o)  # >0 up, <0 down
    num_up = int((dirs > 0).sum())
    num_down = int((dirs < 0).sum())
    feats["num_up_4"] = num_up
    feats["num_down_4"] = num_down

    max_up_streak = 0
    max_down_streak = 0
    up_streak = 0
    down_streak = 0
    for d in dirs:
        if d > 0:
            up_streak += 1
            down_streak = 0
        elif d < 0:
            down_streak += 1
            up_streak = 0
        else:
            up_streak = down_streak = 0
        max_up_streak = max(max_up_streak, up_streak)
        max_down_streak = max(max_down_streak, down_streak)
    feats["streak_up_4"] = max_up_streak
    feats["streak_down_4"] = max_down_streak

    # Path-dependent extremes: intrawindow max up move and max drawdown
    prices = c
    run_max = np.maximum.accumulate(prices)
    run_min = np.minimum.accumulate(prices)
    drawup = (prices - run_min) / np.where(run_min == 0, 1e-9, run_min)
    drawdown = (prices - run_max) / np.where(run_max == 0, 1e-9, run_max)
    feats["max_up_move_4"] = float(drawup.max())
    feats["max_drawdown_4"] = float(drawdown.min())

    # Volatility: range over first 4 minutes relative to starting price
    feats["hl_range_4"] = float((h.max() / l.min()) - 1.0)

    # Volume: intensity of trading vs typical window volume
    total_vol_4 = float(v.sum())
    mean_vol_4 = float(v.mean())
    feats["volume_sum_4"] = total_vol_4
    feats["volume_mean_4"] = mean_vol_4

    # Direction changes: choppiness vs smooth trend
    dir_changes = 0
    for i in range(1, len(dirs)):
        if dirs[i] != 0 and dirs[i - 1] != 0 and dirs[i] != dirs[i - 1]:
            dir_changes += 1
    feats["dir_changes_4"] = float(dir_changes)

    # 1-minute returns and average
    c_ret = pd.Series(c).pct_change().fillna(0.0)
    feats["avg_1m_ret_4"] = float(c_ret.mean())

    # Optional: very short RSI (4) capturing micro overbought/oversold
    delta = c_ret.values
    up = np.clip(delta, 0, None)
    down = -np.clip(delta, None, 0)
    avg_gain = up.mean() if up.size else 0.0
    avg_loss = down.mean() if down.size else 0.0
    if avg_loss == 0:
        rsi_4 = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_4 = 100.0 - (100.0 / (1.0 + rs))
    feats["rsi_4"] = float(rsi_4)

    # Bollinger-style squeeze: tightness of price band vs recent level
    closes = pd.Series(c)
    sma = closes.mean()
    std = closes.std(ddof=0) if closes.size > 1 else 0.0
    band_width = 2 * std / (sma + 1e-9)
    feats["boll_width_4"] = float(band_width)

    # Lightweight MACD: EMA(2) - EMA(5) on 1m closes
    alpha_fast = 2 / (2 + 1)
    alpha_slow = 2 / (5 + 1)
    ema_fast = closes.ewm(alpha=alpha_fast, adjust=False).mean().iloc[-1]
    ema_slow = closes.ewm(alpha=alpha_slow, adjust=False).mean().iloc[-1]
    feats["macd_2_5"] = float(ema_fast - ema_slow)

    # Ensure no NaNs so XGBoost sees clean numeric features
    for k, v in list(feats.items()):
        if not np.isfinite(v):
            feats[k] = 0.0

    return feats


def build_5min_dataset(df_1m: pd.DataFrame) -> pd.DataFrame:
    """
    Build non-overlapping 5-minute windows aligned to calendar 5-minute buckets.

    For each full 5-candle window:
      - label = 1 if last close >= first close else 0
      - features come only from the first 4 candles (early-window momentum)
    """
    if df_1m.empty:
        return pd.DataFrame()

    df = df_1m.copy()
    df["window_start"] = df.index.floor("5min")

    rows = []
    for window_start, group in df.groupby("window_start"):
        group = group.sort_index()
        if len(group) < 5:
            continue

        w = group.iloc[:5].copy()
        first4 = w.iloc[:4]
        start_price = float(first4["close"].iloc[0])
        end_price = float(w["close"].iloc[-1])
        label = int(end_price >= start_price)

        feats = _extract_features_first4(first4)
        feats.update(
            {
                "window_start": window_start,
                "start_price": start_price,
                "end_price": end_price,
                "label": label,
            }
        )
        rows.append(feats)

    out = pd.DataFrame(rows)
    if not out.empty:
        out.set_index("window_start", inplace=True)
        out.sort_index(inplace=True)
    return out


def main():
    cfg = DataConfig()
    files = _find_csvs(cfg)
    if not files:
        logger.error("No Binance CSV files found in %s", cfg.data_dir)
        return

    logger.info("Found %d Binance CSV files", len(files))

    dfs = []
    for f in files:
        logger.info("Loading %s", f.name)
        dfs.append(_load_binance_file(f))

    df_1m = pd.concat(dfs).sort_index()
    df_1m = df_1m[~df_1m.index.duplicated(keep="first")]
    logger.info("Total 1m candles after concat: %d", len(df_1m))

    if df_1m.empty:
        logger.error("No valid 1m candles after cleaning timestamps; nothing to build.")
        return

    logger.info("Building 5-minute windows and features…")
    dataset = build_5min_dataset(df_1m)
    logger.info("Generated %d labeled 5-minute samples", len(dataset))

    out_path = cfg.features_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".parquet", ".pq"):
        dataset.to_parquet(out_path)
    else:
        dataset.to_csv(out_path)

    logger.info("Saved features to %s", out_path)


if __name__ == "__main__":
    main()

