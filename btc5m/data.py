from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import pandas as pd

LOGGER = logging.getLogger(__name__)

KLINE_COLUMNS = [
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

NUMERIC_COLUMNS = [
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


def _detect_unix_unit(ts: pd.Series) -> str:
    """Infer unix timestamp unit from magnitude.

    Binance exports are commonly in ms, but some datasets are in us/ns.
    """
    valid = pd.to_numeric(ts, errors="coerce").dropna()
    if valid.empty:
        return "ms"

    magnitude = float(valid.abs().median())
    if magnitude >= 1e17:
        return "ns"
    if magnitude >= 1e14:
        return "us"
    if magnitude >= 1e11:
        return "ms"
    return "s"


def discover_files(data_dir: Path, patterns: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(sorted(data_dir.glob(pattern)))
    deduped = sorted(set(files))
    LOGGER.info("Discovered %d input files", len(deduped))
    return deduped


def load_klines_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, names=KLINE_COLUMNS)
    if df.empty:
        LOGGER.warning("File has no rows: %s", path)
        return df

    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
    df = df.dropna(subset=["open_time", "open", "high", "low", "close", "volume"])

    unit = _detect_unix_unit(df["open_time"])
    df["open_dt"] = pd.to_datetime(df["open_time"], unit=unit, utc=True, errors="coerce")
    df = df.dropna(subset=["open_dt"])
    df = df.set_index("open_dt").sort_index()
    LOGGER.info("Loaded %s rows=%d unix_unit=%s", path.name, len(df), unit)
    return df


def load_all_klines(data_dir: Path, patterns: Iterable[str]) -> pd.DataFrame:
    files = discover_files(data_dir, patterns)
    if not files:
        raise FileNotFoundError(
            f"No files found under {data_dir} for patterns: {list(patterns)}"
        )

    chunks: list[pd.DataFrame] = []
    for file_path in files:
        try:
            part = load_klines_csv(file_path)
            if not part.empty:
                chunks.append(part)
        except Exception as exc:
            LOGGER.exception("Failed to read %s: %s", file_path, exc)

    if not chunks:
        raise RuntimeError("All CSV reads failed or returned empty data.")

    df = pd.concat(chunks, axis=0).sort_index()
    df = df[~df.index.duplicated(keep="last")]

    # Keep only the columns needed for feature engineering and live consistency.
    keep_cols = [
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
    df = df[keep_cols]
    LOGGER.info("Combined dataframe rows=%d range=[%s .. %s]", len(df), df.index.min(), df.index.max())
    return df
