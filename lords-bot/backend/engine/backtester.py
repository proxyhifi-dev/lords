from __future__ import annotations

from typing import Any

from strategies.orb_strategy import OrbStrategy


class Backtester:
    def __init__(self) -> None:
        self.strategy = OrbStrategy()

    def run(self, candles: list[dict[str, Any]], orb_high: float, orb_low: float, oi_bias: str = 'NEUTRAL') -> dict[str, Any]:
        trades: list[float] = []
        entry = None
        side = ''

        for idx in range(15, len(candles)):
            window = candles[: idx + 1]
            spot = float(window[-1]['close'])
            signal = self.strategy.generate(spot, orb_high, orb_low, oi_bias, window)
            if entry is None and signal.signal in {'BUY','BUY CALL','BUY PUT'}:
                entry = signal.entry_price
                side = signal.option_side
                continue
            if entry is not None:
                pnl = (spot - entry) if side == 'CALL' else (entry - spot)
                if abs(pnl) >= abs(entry * 0.003):
                    trades.append(pnl)
                    entry = None
                    side = ''

        gross_profit = sum(p for p in trades if p > 0)
        gross_loss = abs(sum(p for p in trades if p < 0))
        wins = sum(1 for p in trades if p > 0)
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in trades:
            equity += p
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)

        return {
            'trades': len(trades),
            'win_rate': round((wins / len(trades) * 100), 2) if trades else 0,
            'profit_factor': round(gross_profit / gross_loss, 2) if gross_loss else 0,
            'drawdown': round(abs(max_dd), 2),
            'net_pnl': round(sum(trades), 2),
        }
