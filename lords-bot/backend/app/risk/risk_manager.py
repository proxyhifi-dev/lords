from dataclasses import dataclass

from backend.config import settings


@dataclass
class RiskSnapshot:
    trade_count: int = 0
    realized_pnl: float = 0.0


class RiskManager:
    def __init__(self) -> None:
        self.snapshot = RiskSnapshot()

    def can_trade(self) -> bool:
        if self.snapshot.trade_count >= settings.max_trades_per_day:
            return False
        if self.snapshot.realized_pnl <= -abs(settings.max_daily_loss):
            return False
        return True

    def register_entry(self) -> None:
        self.snapshot.trade_count += 1

    def register_exit(self, pnl: float) -> None:
        self.snapshot.realized_pnl += pnl

    @staticmethod
    def should_stoploss(entry: float, now_price: float) -> bool:
        return now_price <= entry * (1 - settings.stop_loss_pct)

    @staticmethod
    def should_target(entry: float, now_price: float) -> bool:
        return now_price >= entry * (1 + settings.target_pct)
