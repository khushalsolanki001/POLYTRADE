from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

FEATURE_COLUMNS = [
    "mom_1m",
    "mom_2m",
    "mom_3m",
    "mom_4m",
    "up_count_4",
    "down_count_4",
    "max_up_streak_4",
    "max_down_streak_4",
    "max_drawup_4",
    "max_drawdown_4",
    "range_4m",
    "ret_std_4m",
    "vol_sum_4m",
    "vol_rel_4m",
    "taker_buy_ratio_4m",
    "body_range_ratio_mean_4m",
    "wick_body_ratio_mean_4m",
    "direction_changes_4m",
    "lag_ret_prev1",
    "lag_ret_prev2",
    "ret_1_to_2",
    "ret_2_to_3",
    "ret_3_to_4",
    "yes_price_proxy",
]


def _safe_div(a: float, b: float, eps: float = 1e-9) -> float:
    return float(a) / float(b + eps)


def _max_streak(flags: list[int], value: int) -> int:
    best = 0
    current = 0
    for f in flags:
        if f == value:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _estimate_yes_price_proxy(mom_4m: float) -> float:
    # Proxy for historical backtests when true Polymarket quote is unavailable.
    score = 6.0 * mom_4m
    prob = 1.0 / (1.0 + math.exp(-score))
    return float(np.clip(prob, 0.05, 0.95))


def _window_has_full_minutes(window: pd.DataFrame) -> bool:
    if len(window) != 5:
        return False
    diffs = window.index.to_series().diff().dropna()
    return bool((diffs == pd.Timedelta(minutes=1)).all())


def extract_feature_row(
    window: pd.DataFrame,
    history_before_window: pd.DataFrame,
) -> dict[str, Any]:
    first4 = window.iloc[:4]
    c = first4["close"].to_numpy(dtype=float)
    o = first4["open"].to_numpy(dtype=float)

    # Momentum persistence is strongest in the opening minutes of the 5m market.
    # These capture directional impulse as soon as the window opens.
    start_close = float(c[0])
    mom_1m = _safe_div(c[1], c[0]) - 1.0
    mom_2m = _safe_div(c[2], c[0]) - 1.0
    mom_3m = _safe_div(c[3], c[0]) - 1.0
    mom_4m = _safe_div(c[3], o[0]) - 1.0

    # Candle direction counts and streaks help separate trend continuation from chop.
    dirs = np.where(first4["close"].to_numpy() >= first4["open"].to_numpy(), 1, -1)
    up_count = int((dirs == 1).sum())
    down_count = int((dirs == -1).sum())
    max_up_streak = _max_streak(dirs.tolist(), 1)
    max_down_streak = _max_streak(dirs.tolist(), -1)

    # Intrawindow excursion features capture how far price pushed up/down in minute 1-4.
    max_drawup = float(first4["high"].max() / start_close - 1.0)
    max_drawdown = float(first4["low"].min() / start_close - 1.0)

    # Volatility and path roughness reduce false confidence in noisy tape.
    range_4m = float((first4["high"].max() - first4["low"].min()) / start_close)
    returns = np.diff(c) / (c[:-1] + 1e-9)
    ret_std_4m = float(np.std(returns, ddof=0))

    # Relative volume and taker-buy ratio track urgency from aggressive participants.
    vol_sum = float(first4["volume"].sum())
    prev_vol_avg = float(history_before_window["volume"].tail(30).mean()) if not history_before_window.empty else np.nan
    vol_rel = _safe_div(vol_sum, prev_vol_avg * 4.0) if np.isfinite(prev_vol_avg) and prev_vol_avg > 0 else 1.0

    taker_buy = float(first4["taker_buy_base"].sum()) if "taker_buy_base" in first4.columns else 0.0
    taker_buy_ratio = _safe_div(taker_buy, vol_sum)

    # Body/wick structure approximates microstructure conviction vs rejection.
    body = (first4["close"] - first4["open"]).abs()
    full_range = (first4["high"] - first4["low"]).clip(lower=1e-9)
    wick = (full_range - body).clip(lower=0.0)
    body_range_ratio = (body / full_range).mean()
    wick_body_ratio = (wick / (body + 1e-9)).mean()

    # Direction flips within first 4 candles are useful chop / mean-reversion indicators.
    direction_changes = int(np.sum(np.abs(np.diff(dirs)) > 0))

    # Lagged returns from pre-window candles add immediate context without leaking candle 5.
    lag_ret_prev1 = 0.0
    lag_ret_prev2 = 0.0
    if len(history_before_window) >= 2:
        p1 = history_before_window["close"].iloc[-1]
        p0 = history_before_window["close"].iloc[-2]
        lag_ret_prev1 = float(_safe_div(p1, p0) - 1.0)
    if len(history_before_window) >= 3:
        p2 = history_before_window["close"].iloc[-3]
        p1 = history_before_window["close"].iloc[-2]
        lag_ret_prev2 = float(_safe_div(p1, p2) - 1.0)

    ret_1_to_2 = float(_safe_div(c[1], c[0]) - 1.0)
    ret_2_to_3 = float(_safe_div(c[2], c[1]) - 1.0)
    ret_3_to_4 = float(_safe_div(c[3], c[2]) - 1.0)

    yes_price_proxy = _estimate_yes_price_proxy(mom_4m)

    return {
        "mom_1m": mom_1m,
        "mom_2m": mom_2m,
        "mom_3m": mom_3m,
        "mom_4m": mom_4m,
        "up_count_4": up_count,
        "down_count_4": down_count,
        "max_up_streak_4": max_up_streak,
        "max_down_streak_4": max_down_streak,
        "max_drawup_4": max_drawup,
        "max_drawdown_4": max_drawdown,
        "range_4m": range_4m,
        "ret_std_4m": ret_std_4m,
        "vol_sum_4m": vol_sum,
        "vol_rel_4m": vol_rel,
        "taker_buy_ratio_4m": taker_buy_ratio,
        "body_range_ratio_mean_4m": float(body_range_ratio),
        "wick_body_ratio_mean_4m": float(wick_body_ratio),
        "direction_changes_4m": direction_changes,
        "lag_ret_prev1": lag_ret_prev1,
        "lag_ret_prev2": lag_ret_prev2,
        "ret_1_to_2": ret_1_to_2,
        "ret_2_to_3": ret_2_to_3,
        "ret_3_to_4": ret_3_to_4,
        "yes_price_proxy": yes_price_proxy,
    }


def build_feature_dataset(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    grouped = df.groupby(pd.Grouper(freq="5min", label="left", closed="left"))
    for window_start, window in grouped:
        if window.empty or not _window_has_full_minutes(window):
            continue

        history = df[df.index < window_start]
        feat = extract_feature_row(window=window, history_before_window=history)

        start_price = float(window["close"].iloc[0])
        end_price = float(window["close"].iloc[4])
        label = int(end_price >= start_price)

        rows.append(
            {
                "window_start": window_start,
                "start_price": start_price,
                "end_price": end_price,
                "label": label,
                **feat,
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        raise RuntimeError("No full 5-minute windows produced features.")

    out = out.sort_values("window_start").reset_index(drop=True)
    out = out.dropna(axis=0)
    return out


def extract_live_feature_vector(df: pd.DataFrame, window_start: pd.Timestamp) -> pd.DataFrame:
    window = df[(df.index >= window_start) & (df.index < window_start + pd.Timedelta(minutes=5))].copy()
    if len(window) < 4:
        raise ValueError("Need at least 4 completed 1-minute candles in the active window.")

    # Use first four candles only and append a placeholder row to reuse the same transform path.
    first4 = window.iloc[:4].copy()
    if len(first4) < 4:
        raise ValueError("Insufficient first-4 candles.")

    placeholder = first4.iloc[[-1]].copy()
    reusable_window = pd.concat([first4, placeholder], axis=0)
    history = df[df.index < window_start]
    feat = extract_feature_row(reusable_window, history)

    return pd.DataFrame([feat], columns=FEATURE_COLUMNS)
