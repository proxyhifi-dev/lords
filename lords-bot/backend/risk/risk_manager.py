from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from config import settings
from models import EngineState

IST = ZoneInfo('Asia/Kolkata')


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = 'ok'
    quantity: int = 0


class RiskManager:
    def __init__(self) -> None:
        self.max_daily_loss = settings.max_daily_loss
        self.max_trades_per_day = settings.max_trades_per_day
        self.max_position_size = settings.max_position_size
        self.square_off_time = time(15, 20)


    def validate_trade(self, state, qty: int) -> RiskDecision:
        pnl = float(getattr(state, 'pnl', getattr(state, 'realized_pnl', 0.0)) or 0.0)
        trade_count = int(getattr(state, 'trade_count', getattr(state, 'trades_today', 0)) or 0)
        if pnl <= -abs(self.max_daily_loss):
            return RiskDecision(False, 'max_daily_loss_hit', 0)
        if trade_count >= self.max_trades_per_day:
            return RiskDecision(False, 'max_trades_hit', 0)
        if qty > self.max_position_size:
            return RiskDecision(False, 'max_position_size_exceeded', 0)
        return RiskDecision(True, 'ok', qty)
    def pre_trade_check(self, state: EngineState, capital: float, entry: float, stop: float) -> RiskDecision:
        if state.consecutive_losses >= 3:
            return RiskDecision(False, 'consecutive_loss_limit')
        if state.realized_pnl <= -abs(self.max_daily_loss):
            return RiskDecision(False, 'daily_loss_limit')
        if state.trades_today >= self.max_trades_per_day:
            return RiskDecision(False, 'max_trades_per_day')
        risk_per_unit = max(abs(entry - stop), max(entry * 0.02, 1.0))
        capital_risk = max(1000.0, capital * 0.01)
        quantity = int(capital_risk / risk_per_unit)
        quantity = max(settings.lot_size, (quantity // settings.lot_size) * settings.lot_size)
        quantity = min(quantity, self.max_position_size)
        return RiskDecision(quantity > 0, 'ok' if quantity > 0 else 'size_zero', quantity=quantity)

    def should_exit_trade(self, position: dict, spot: float, option_ltp: float) -> bool:
        stop_loss = float(position.get('stop_loss') or 0.0)
        target = float(position.get('target') or 0.0)
        if stop_loss and option_ltp <= stop_loss:
            return True
        if target and option_ltp >= target:
            return True
        now = datetime.now(IST).time()
        return now >= self.square_off_time

    def circuit_breaker(self, state: EngineState, broker_ok: bool, api_ok: bool) -> str:
        if not broker_ok:
            return 'BROKER_DOWN'
        if not api_ok:
            return 'API_DOWN'
        if state.realized_pnl <= -abs(self.max_daily_loss):
            return 'DAILY_LOSS_LIMIT'
        return 'RUNNING'
