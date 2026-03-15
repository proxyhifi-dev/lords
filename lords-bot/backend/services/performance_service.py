from __future__ import annotations

from typing import Any


class PerformanceService:
    def summarize(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(trades)
        wins = sum(1 for t in trades if float(t.get('pnl', 0)) > 0)
        total_pnl = sum(float(t.get('pnl', 0)) for t in trades)
        peak = 0.0
        max_dd = 0.0
        equity = 0.0
        for trade in trades:
            equity += float(trade.get('pnl', 0))
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)

        return {
            'total_trades': total,
            'win_rate': round((wins / total * 100), 2) if total else 0.0,
            'daily_pnl': round(total_pnl, 2),
            'max_drawdown': round(abs(max_dd), 2),
        }
