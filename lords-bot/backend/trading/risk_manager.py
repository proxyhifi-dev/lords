from __future__ import annotations

from datetime import date

from config import settings
from runtime_state import runtime_state


class RiskManager:

    def _roll_day(self):

        today = date.today()

        if runtime_state.trade_day != today:

            runtime_state.trade_day = today

            runtime_state.orders.clear()

            runtime_state.day_pnl = 0.0

    def can_trade(self):

        self._roll_day()

        trades_today = len(runtime_state.orders)

        if trades_today >= settings.max_trades_per_day:

            return False, "MAX_TRADES_PER_DAY reached"

        if runtime_state.day_pnl <= -abs(settings.max_daily_loss):

            return False, "MAX_DAILY_LOSS reached"

        return True, "ok"

    def position_size(self, entry_price, stop_loss_price, lot_size=50):

        risk_budget = abs(settings.max_daily_loss) * 0.02

        per_unit_risk = max(1.0, abs(entry_price - stop_loss_price))

        qty = int(risk_budget / per_unit_risk)

        lots = max(1, qty // lot_size)

        return lots * lot_size


risk_manager = RiskManager()