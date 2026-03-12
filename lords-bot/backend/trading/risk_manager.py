from __future__ import annotations

from datetime import date

from backend.config import settings


class RiskManager:
    def __init__(self) -> None:
        self.trade_count = 0
        self.realized_pnl = 0.0
        self._day = date.today()

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self.trade_count = 0
            self.realized_pnl = 0.0

    def can_trade(self) -> tuple[bool, str]:
        self._roll_day()
        if self.trade_count >= settings.max_trades_per_day:
            return False, 'MAX_TRADES_PER_DAY reached'
        if self.realized_pnl <= -abs(settings.max_daily_loss):
            return False, 'MAX_DAILY_LOSS reached'
        return True, 'ok'

    def register_trade(self) -> None:
        self._roll_day()
        self.trade_count += 1


risk_manager = RiskManager()
