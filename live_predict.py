import asyncio
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from binance.client import Client
from binance.streams import ThreadedWebsocketManager
from xgboost import XGBClassifier

from ml_config import LiveConfig, LOG_DIR
from data_and_features import _extract_features_first4


def _setup_logger():
    import logging

    logger = logging.getLogger("live_predict")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(LOG_DIR / "live_predict.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


logger = _setup_logger()


@dataclass
class PaperPosition:
    window_start: datetime
    yes_price: float
    size: float
    start_price: float
    prob_up: float
    opened_at: datetime
    resolved: bool = False
    outcome: Optional[int] = None
    pnl: Optional[float] = None


class PaperEngine:
    """
    Paper trading engine for Polymarket-style BTC 5-minute YES bets.
    Enforces per-trade and daily loss limits so the strategy doesn't
    martingale itself to death when volatility spikes.
    """

    def __init__(self, cfg: LiveConfig):
        self.cfg = cfg
        self.balance: float = cfg.starting_balance
        self.starting_balance: float = cfg.starting_balance
        self.positions: List[PaperPosition] = []
        self.day_start = datetime.now(tz=timezone.utc).date()
        self.day_start_balance = self.balance

    def _reset_day_if_needed(self, now: datetime):
        if now.date() != self.day_start:
            self.day_start = now.date()
            self.day_start_balance = self.balance

    def daily_loss_exceeded(self) -> bool:
        dd = (self.balance - self.day_start_balance) / self.day_start_balance
        return dd <= -self.cfg.daily_loss_limit_fraction

    def max_risk_amount(self) -> float:
        return self.balance * self.cfg.max_risk_fraction

    def open_position(
        self,
        window_start: datetime,
        yes_price: float,
        prob_up: float,
        start_price: float,
    ) -> Optional[PaperPosition]:
        now = datetime.now(tz=timezone.utc)
        self._reset_day_if_needed(now)
        if self.daily_loss_exceeded():
            logger.warning("Daily loss limit hit; skipping new trades.")
            return None

        risk = self.max_risk_amount()
        if risk <= 0:
            return None

        # Risk per contract ~ yes_price (worst case)
        size = risk / yes_price
        pos = PaperPosition(
            window_start=window_start,
            yes_price=yes_price,
            size=size,
            start_price=start_price,
            prob_up=prob_up,
            opened_at=now,
        )
        self.positions.append(pos)
        logger.info(
            "Opened paper YES | window=%s prob_up=%.3f price=%.3f size=%.2f balance=%.2f",
            window_start,
            prob_up,
            yes_price,
            size,
            self.balance,
        )
        return pos

    def resolve_positions(self, window_start: datetime, end_price: float):
        """
        Resolve all positions for a finished 5-minute window using Binance
        close as a proxy for the Chainlink snapshot.
        """
        for pos in self.positions:
            if pos.window_start != window_start or pos.resolved:
                continue

            label = int(end_price >= pos.start_price)
            pnl_per_contract = (1.0 - pos.yes_price) if label == 1 else -pos.yes_price
            pnl = pnl_per_contract * pos.size

            pos.resolved = True
            pos.outcome = label
            pos.pnl = pnl
            self.balance += pnl

            logger.info(
                "Resolved | window=%s outcome=%d pnl=%.2f new_balance=%.2f",
                window_start,
                label,
                pnl,
                self.balance,
            )

    def stats(self) -> Dict[str, float]:
        settled = [p for p in self.positions if p.resolved]
        if not settled:
            return {
                "num_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "balance": self.balance,
            }
        wins = sum(1 for p in settled if p.outcome == 1 and (p.pnl or 0.0) > 0)
        win_rate = wins / len(settled)
        total_pnl = sum(p.pnl or 0.0 for p in settled)
        return {
            "num_trades": len(settled),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "balance": self.balance,
        }


class Binance1mStream:
    """
    Maintains a rolling 1-minute BTCUSDT kline buffer using Binance websocket.
    """

    def __init__(self, cfg: LiveConfig):
        self.cfg = cfg
        self.client = Client(api_key=cfg.binance_api_key, api_secret=cfg.binance_api_secret)
        self.twm: Optional[ThreadedWebsocketManager] = None
        self.df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        self.lock = asyncio.Lock()

    def _on_kline(self, msg):
        if msg.get("e") != "kline":
            return
        k = msg["k"]
        if not k["x"]:
            return  # only closed candles

        ts = datetime.fromtimestamp(k["t"] / 1000.0, tz=timezone.utc)
        row = {
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }

        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(asyncio.create_task, self._update_df(ts, row))

    async def _update_df(self, ts: datetime, row: Dict[str, float]):
        async with self.lock:
            self.df.loc[ts] = row
            cutoff = ts - pd.Timedelta(minutes=30)
            self.df = self.df[self.df.index >= cutoff]

    async def start(self):
        self.twm = ThreadedWebsocketManager(
            api_key=self.cfg.binance_api_key,
            api_secret=self.cfg.binance_api_secret,
        )
        self.twm.start()
        self.twm.start_kline_socket(
            callback=self._on_kline,
            symbol=self.cfg.binance_symbol,
            interval=self.cfg.binance_interval,
        )
        logger.info("Started Binance 1m kline websocket")

    async def stop(self):
        if self.twm:
            self.twm.stop()
            logger.info("Stopped Binance websocket")

    async def get_1m(self) -> pd.DataFrame:
        async with self.lock:
            return self.df.sort_index().copy()


def _load_model(path: str) -> XGBClassifier:
    model = XGBClassifier()
    model.load_model(path)
    return model


def _current_window_start(ts: datetime, window_minutes: int) -> datetime:
    minute = (ts.minute // window_minutes) * window_minutes
    return ts.replace(second=0, microsecond=0, minute=minute)


def _get_polymarket_yes_price(cfg: LiveConfig) -> Optional[float]:
    """
    Fetch current YES price (mid) for the active BTC 5-minute Up/Down market.
    This is a heuristic based on the Gamma + CLOB APIs and may need tweaking
    as market naming evolves.
    """
    try:
        r = requests.get(cfg.polymarket_gamma_url, timeout=5)
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        logger.error("Gamma API error: %s", e)
        return None

    if not markets:
        return None

    market = markets[0]
    market_id = market.get("id")
    if not market_id:
        return None

    try:
        ob_resp = requests.get(f"{cfg.polymarket_clob_url}?market={market_id}", timeout=5)
        ob_resp.raise_for_status()
        ob = ob_resp.json()
    except Exception as e:
        logger.error("CLOB API error: %s", e)
        return None

    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    if not bids or not asks:
        return None

    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    mid = (best_bid + best_ask) / 2.0
    return mid


async def _extract_live_features(
    df_1m: pd.DataFrame,
    window_start: datetime,
    cfg: LiveConfig,
) -> Optional[Tuple[Dict[str, float], float]]:
    window_end = window_start + pd.Timedelta(minutes=cfg.window_minutes)
    mask = (df_1m.index >= window_start) & (df_1m.index < window_end)
    w = df_1m.loc[mask].sort_index()
    if len(w) < cfg.min_candles_in_window:
        return None
    first4 = w.iloc[:4].copy()
    start_price = float(first4["close"].iloc[0])
    feats = _extract_features_first4(first4)
    return feats, start_price


async def live_loop():
    cfg = LiveConfig()

    if not cfg.model_path.exists():
        logger.error("Model file not found: %s", cfg.model_path)
        return

    model = _load_model(str(cfg.model_path))
    stream = Binance1mStream(cfg)
    engine = PaperEngine(cfg)

    await stream.start()
    logger.info("Live ML paper-trading loop started")

    try:
        while True:
            now = datetime.now(tz=timezone.utc)
            window_start = _current_window_start(now, cfg.window_minutes)

            df_1m = await stream.get_1m()
            if df_1m.empty:
                await asyncio.sleep(cfg.polling_interval_sec)
                continue

            feat_res = await _extract_live_features(df_1m, window_start, cfg)
            if feat_res is None:
                await asyncio.sleep(cfg.polling_interval_sec)
                continue
            feats, start_price = feat_res

            # Ensure feature order is consistent with training (sorted keys)
            feature_cols = sorted(feats.keys())
            x = np.array([[feats[c] for c in feature_cols]], dtype=float)
            prob_up = float(model.predict_proba(x)[0, 1])

            yes_price = _get_polymarket_yes_price(cfg)
            if yes_price is None:
                logger.warning("Could not fetch Polymarket YES price.")
                await asyncio.sleep(cfg.polling_interval_sec)
                continue

            edge = prob_up - yes_price
            logger.info(
                "Window %s | prob_up=%.3f yes_price=%.3f edge=%.3f",
                window_start,
                prob_up,
                yes_price,
                edge,
            )

            if prob_up > cfg.prob_threshold and edge > cfg.edge_buffer:
                engine.open_position(
                    window_start=window_start,
                    yes_price=yes_price,
                    prob_up=prob_up,
                    start_price=start_price,
                )

            # Resolve once we have 5 full candles inside this 5-minute window
            window_end = window_start + pd.Timedelta(minutes=cfg.window_minutes)
            mask_res = (df_1m.index >= window_start) & (df_1m.index < window_end)
            w_res = df_1m.loc[mask_res].sort_index()
            if len(w_res) >= 5:
                end_price = float(w_res["close"].iloc[4])
                engine.resolve_positions(window_start, end_price)

            # Periodic stats + JSONL log for later analysis
            if len(engine.positions) % 20 == 0:
                logger.info("Paper stats: %s", engine.stats())

            if engine.positions:
                last = engine.positions[-1]
                try:
                    line = json.dumps(asdict(last), default=str)
                    print(line)
                except Exception:
                    pass

            await asyncio.sleep(cfg.polling_interval_sec)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        await stream.stop()
        logger.info("Final stats: %s", engine.stats())


if __name__ == "__main__":
    asyncio.run(live_loop())

