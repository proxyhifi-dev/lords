from __future__ import annotations

from datetime import date

from config import settings
from runtime_state import runtime_state


class RiskManager:
    def _roll_day(self) -> None:
        today = date.today()
        if runtime_state.trade_day != today:
            runtime_state.trade_day = today
            runtime_state.orders.clear()
            runtime_state.day_pnl = 0.0

    def can_trade(self) -> tuple[bool, str]:
        self._roll_day()
        trades_today = len(runtime_state.orders)
        if trades_today >= settings.max_trades_per_day:
            return False, 'MAX_TRADES_PER_DAY reached'
        if runtime_state.day_pnl <= -abs(settings.max_daily_loss):
            return False, 'MAX_DAILY_LOSS reached'
        return True, 'ok'


risk_manager = RiskManager()
