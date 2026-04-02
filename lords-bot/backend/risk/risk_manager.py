from __future__ import annotations

from dataclasses import dataclass

from config import settings
from services.state_manager import RuntimeState


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = 'ok'


class RiskManager:
    def __init__(self) -> None:
        self.max_daily_loss = settings.max_daily_loss
        self.max_trades_per_day = settings.max_trades_per_day
        self.max_position_size = settings.max_position_size

    def validate_trade(self, state: RuntimeState, qty: int) -> RiskDecision:
        if abs(state.pnl) >= self.max_daily_loss and state.pnl < 0:
            return RiskDecision(False, 'max_daily_loss_hit')
        if state.trade_count >= self.max_trades_per_day:
            return RiskDecision(False, 'max_trades_hit')
        if qty > self.max_position_size:
            return RiskDecision(False, 'max_position_size_exceeded')
        return RiskDecision(True)
