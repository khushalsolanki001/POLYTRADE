from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from binance import AsyncClient, BinanceSocketManager
from xgboost import XGBClassifier

from .config import AppConfig, build_default_config
from .features import FEATURE_COLUMNS, extract_live_feature_vector
from .paper import PaperEngine

LOGGER = logging.getLogger(__name__)


def setup_logging(log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=handlers,
    )


def floor_to_5m(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.floor("5min")


def fetch_polymarket_yes_price(cfg: AppConfig) -> float | None:
    # Gamma responses vary by market shape, so parsing is defensive.
    try:
        resp = requests.get(
            cfg.live.gamma_markets_url,
            params={"limit": 200, "active": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()

        if isinstance(payload, dict):
            items = payload.get("markets") or payload.get("data") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []

        slug = cfg.live.polymarket_event_slug.lower()
        for item in items:
            text = " ".join(
                str(item.get(k, ""))
                for k in ("slug", "eventSlug", "question", "title")
            ).lower()
            if slug not in text:
                continue

            outcome_prices = item.get("outcomePrices")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = None

            if isinstance(outcome_prices, list) and outcome_prices:
                first = float(outcome_prices[0])
                return float(np.clip(first, 0.01, 0.99))

            yes_ask = item.get("bestAsk") or item.get("yesPrice")
            if yes_ask is not None:
                return float(np.clip(float(yes_ask), 0.01, 0.99))

        return None
    except Exception as exc:
        LOGGER.warning("Polymarket quote fetch failed: %s", exc)
        return None


class LiveBTC5MPaperTrader:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.model = XGBClassifier()
        self.model.load_model(cfg.model.model_out_json)

        self.paper = PaperEngine(
            starting_bankroll=cfg.live.starting_bankroll,
            fixed_fraction_per_trade=cfg.live.fixed_fraction_per_trade,
        )

        self.df = pd.DataFrame(
            columns=[
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
        )
        self.predicted_windows: set[str] = set()

    def _append_closed_kline(self, payload: dict[str, Any]) -> None:
        k = payload.get("k", {})
        if not k.get("x", False):
            return

        open_time_ms = int(k["t"])
        dt = pd.to_datetime(open_time_ms, unit="ms", utc=True)

        row = {
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "quote_volume": float(k["q"]),
            "trades": float(k["n"]),
            "taker_buy_base": float(k.get("V", 0.0)),
            "taker_buy_quote": float(k.get("Q", 0.0)),
        }

        self.df.loc[dt] = row
        self.df = self.df.sort_index().tail(240)
        LOGGER.info("Closed 1m candle stored @ %s close=%.2f", dt, row["close"])

    def _resolve_finished_windows(self, now: pd.Timestamp) -> None:
        to_resolve = []
        for window_start in list(self.paper.open_positions.keys()):
            ws = pd.Timestamp(window_start)
            if now >= ws + pd.Timedelta(minutes=5):
                to_resolve.append(ws)

        for ws in to_resolve:
            window = self.df[(self.df.index >= ws) & (self.df.index < ws + pd.Timedelta(minutes=5))]
            if len(window) < 5:
                continue

            start_price = float(window["close"].iloc[0])
            end_price = float(window["close"].iloc[-1])
            resolved_up = end_price >= start_price
            pnl = self.paper.resolve(str(ws), resolved_up=resolved_up)
            LOGGER.info(
                "Resolved window=%s start=%.2f end=%.2f up=%s pnl=%.4f bankroll=%.2f",
                ws,
                start_price,
                end_price,
                resolved_up,
                pnl,
                self.paper.bankroll,
            )

    def _maybe_trade_active_window(self) -> None:
        now = pd.Timestamp(datetime.now(timezone.utc))
        window_start = floor_to_5m(now)
        window_key = str(window_start)

        if window_key in self.predicted_windows:
            return

        active_window = self.df[
            (self.df.index >= window_start)
            & (self.df.index < window_start + pd.Timedelta(minutes=5))
        ]
        if len(active_window) < 4:
            return

        try:
            x_live = extract_live_feature_vector(self.df, window_start=window_start)
            x_live = x_live[FEATURE_COLUMNS]
        except Exception as exc:
            LOGGER.debug("Live feature extraction skipped: %s", exc)
            return

        prob_up = float(self.model.predict_proba(x_live)[0, 1])
        yes_price = fetch_polymarket_yes_price(self.cfg)
        if yes_price is None:
            yes_price = self.cfg.sim.default_yes_price

        should_buy = (
            prob_up > self.cfg.sim.buy_probability_threshold
            and yes_price < (prob_up - self.cfg.sim.min_edge_threshold)
        )

        LOGGER.info(
            "Inference window=%s prob_up=%.4f yes_price=%.4f decision=%s",
            window_start,
            prob_up,
            yes_price,
            "BUY_YES" if should_buy else "SKIP",
        )

        if should_buy and window_key not in self.paper.open_positions:
            pos = self.paper.open_yes(
                window_start=window_key,
                yes_price=yes_price,
                prob_up=prob_up,
            )
            LOGGER.info(
                "Paper BUY YES window=%s stake=%.2f shares=%.4f yes_price=%.4f",
                window_key,
                pos.stake,
                pos.shares,
                pos.yes_price,
            )

        self.predicted_windows.add(window_key)

    async def run(self) -> None:
        client = await AsyncClient.create()
        try:
            bsm = BinanceSocketManager(client)
            socket = bsm.kline_socket(symbol=self.cfg.live.symbol.lower(), interval="1m")
            LOGGER.info("Starting websocket for %s", self.cfg.live.symbol)

            async with socket as stream:
                while True:
                    msg = await stream.recv()
                    self._append_closed_kline(msg)

                    now = pd.Timestamp(datetime.now(timezone.utc))
                    self._resolve_finished_windows(now)
                    self._maybe_trade_active_window()
        finally:
            await client.close_connection()


def main() -> None:
    cfg = build_default_config()
    setup_logging(cfg.live.log_file)

    if not cfg.model.model_out_json.exists():
        raise FileNotFoundError(
            f"Model not found at {cfg.model.model_out_json}. Run training first."
        )

    trader = LiveBTC5MPaperTrader(cfg)
    asyncio.run(trader.run())


if __name__ == "__main__":
    main()
