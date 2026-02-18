from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PnLTracker:
    initial_capital: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def current_capital(self) -> float:
        return self.initial_capital + self.realized_pnl

    def record_realized(self, pnl: float) -> None:
        self.realized_pnl += pnl

    def update_unrealized(self, pnl: float) -> None:
        self.unrealized_pnl = pnl

    def reset_daily(self, capital: float | None = None) -> None:
        if capital is not None:
            self.initial_capital = capital
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0

    def snapshot(self) -> dict[str, float]:
        return {
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "current_capital": round(self.current_capital, 2),
        }
