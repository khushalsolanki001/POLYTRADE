from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class PaperPosition:
    window_start: str
    yes_price: float
    stake: float
    shares: float
    prob_up: float


class PaperEngine:
    def __init__(self, starting_bankroll: float, fixed_fraction_per_trade: float) -> None:
        self.starting_bankroll = float(starting_bankroll)
        self.bankroll = float(starting_bankroll)
        self.fixed_fraction_per_trade = float(fixed_fraction_per_trade)
        self.open_positions: dict[str, PaperPosition] = {}
        self.realized_pnl: list[float] = []
        self.trade_log: list[dict[str, Any]] = []

    def open_yes(self, window_start: str, yes_price: float, prob_up: float) -> PaperPosition:
        yes_price = float(np.clip(yes_price, 0.01, 0.99))
        stake = max(0.0, self.bankroll * self.fixed_fraction_per_trade)
        shares = stake / yes_price

        pos = PaperPosition(
            window_start=window_start,
            yes_price=yes_price,
            stake=stake,
            shares=shares,
            prob_up=prob_up,
        )
        self.open_positions[window_start] = pos
        return pos

    def resolve(self, window_start: str, resolved_up: bool) -> float:
        pos = self.open_positions.pop(window_start, None)
        if pos is None:
            return 0.0

        payout = pos.shares if resolved_up else 0.0
        pnl = payout - pos.stake
        self.bankroll += pnl
        self.realized_pnl.append(float(pnl))
        self.trade_log.append(
            {
                "window_start": window_start,
                "yes_price": pos.yes_price,
                "stake": pos.stake,
                "shares": pos.shares,
                "prob_up": pos.prob_up,
                "resolved_up": int(resolved_up),
                "pnl": float(pnl),
                "bankroll": float(self.bankroll),
            }
        )
        return float(pnl)

    def stats(self) -> dict[str, float]:
        if not self.realized_pnl:
            return {
                "starting_bankroll": self.starting_bankroll,
                "bankroll": self.bankroll,
                "num_trades": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "cum_pnl": 0.0,
                "sharpe": 0.0,
            }

        pnl = np.array(self.realized_pnl, dtype=float)
        wins = (pnl > 0).mean()
        sharpe = 0.0
        if pnl.std(ddof=1) > 0:
            sharpe = float((pnl.mean() / pnl.std(ddof=1)) * np.sqrt(len(pnl)))

        return {
            "starting_bankroll": float(self.starting_bankroll),
            "bankroll": float(self.bankroll),
            "num_trades": int(len(pnl)),
            "win_rate": float(wins),
            "avg_pnl": float(pnl.mean()),
            "cum_pnl": float(pnl.sum()),
            "sharpe": float(sharpe),
        }
