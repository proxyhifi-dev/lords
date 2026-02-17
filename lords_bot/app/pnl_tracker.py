from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PnLTracker:
    initial_capital: float
    total_pnl: float = 0.0

    @property
    def current_capital(self) -> float:
        return self.initial_capital + self.total_pnl

    def record_trade(self, pnl: float) -> None:
        self.total_pnl += pnl

    def reset(self, capital: float | None = None) -> None:
        if capital is not None:
            self.initial_capital = capital
        self.total_pnl = 0.0
