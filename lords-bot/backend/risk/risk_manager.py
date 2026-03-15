from __future__ import annotations

from dataclasses import dataclass

from config import settings
from models import EngineState


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    quantity: int = 0


class RiskManager:
    def pre_trade_check(self, state: EngineState, capital: float, entry: float, stop: float) -> RiskDecision:
        if state.system_status != 'RUNNING':
            return RiskDecision(False, f'system_stopped:{state.system_status}')
        if state.active_trade:
            return RiskDecision(False, 'active_trade_lock')
        if state.trades_today >= settings.max_trades_per_day:
            return RiskDecision(False, 'max_trades_reached')
        if state.realized_pnl <= -abs(settings.max_daily_loss):
            return RiskDecision(False, 'daily_loss_limit_hit')
        if state.consecutive_losses >= settings.max_consecutive_losses:
            return RiskDecision(False, 'max_consecutive_losses')
        if capital <= 0:
            return RiskDecision(False, 'invalid_capital')
        if entry <= 0 or stop <= 0:
            return RiskDecision(False, 'invalid_entry_or_stop')

        per_unit_risk = abs(entry - stop)
        if per_unit_risk < 0.01:
            return RiskDecision(False, 'invalid_risk_distance')

        risk_amount = capital * (settings.risk_per_trade_pct / 100)
        qty = int(risk_amount / per_unit_risk)
        qty = max(settings.default_lot_size, qty)
        qty = min(settings.max_position_size, qty)
        qty = max(settings.default_lot_size, (qty // settings.default_lot_size) * settings.default_lot_size)
        return RiskDecision(True, 'ok', qty)

    def circuit_breaker(self, state: EngineState, broker_ok: bool, api_ok: bool) -> str:
        if state.realized_pnl <= -abs(settings.max_daily_loss):
            return 'DAILY_LOSS_LIMIT'
        if state.consecutive_losses >= settings.max_consecutive_losses:
            return 'CONSECUTIVE_LOSS_LIMIT'
        if not broker_ok:
            return 'BROKER_DISCONNECTED'
        if not api_ok:
            return 'API_UNSTABLE'
        return 'RUNNING'
