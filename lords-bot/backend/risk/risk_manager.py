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
    def emergency_stop(self, state: EngineState, reason: str) -> None:
        state.system_status = 'EMERGENCY_STOP'
        state.last_error = reason

    def compute_position_size(self, capital: float, entry: float, stop: float) -> int:
        per_unit_risk = abs(entry - stop)
        if capital <= 0 or per_unit_risk < 0.01:
            return 0
        risk_amount = capital * (settings.risk_per_trade_pct / 100)
        qty = int(risk_amount / per_unit_risk)
        qty = max(settings.default_lot_size, qty)
        qty = min(settings.max_position_size, qty)
        return max(settings.default_lot_size, (qty // settings.default_lot_size) * settings.default_lot_size)

    def _drawdown_guard(self, state: EngineState) -> bool:
        equity = state.realized_pnl
        state.peak_equity = max(state.peak_equity, equity)
        drawdown = state.peak_equity - equity
        state.max_drawdown_hit = drawdown >= abs(settings.max_drawdown)
        return state.max_drawdown_hit

    def pre_trade_check(self, state: EngineState, capital: float, entry: float, stop: float) -> RiskDecision:
        if state.system_status not in {'RUNNING', 'MARKET_CLOSED'}:
            return RiskDecision(False, f'system_stopped:{state.system_status}')
        if state.active_trade:
            return RiskDecision(False, 'active_trade_lock')
        if state.trades_today >= settings.max_trades_per_day:
            return RiskDecision(False, 'max_trades_reached')
        if state.realized_pnl <= -abs(settings.max_daily_loss):
            return RiskDecision(False, 'daily_loss_limit_hit')
        if self._drawdown_guard(state):
            return RiskDecision(False, 'max_drawdown_limit_hit')
        if state.consecutive_losses >= settings.max_consecutive_losses:
            return RiskDecision(False, 'max_consecutive_losses')
        if entry <= 0 or stop <= 0:
            return RiskDecision(False, 'invalid_entry_or_stop')

        qty = self.compute_position_size(capital, entry, stop)
        if qty <= 0:
            return RiskDecision(False, 'invalid_risk_distance')
        return RiskDecision(True, 'ok', qty)

    def circuit_breaker(self, state: EngineState, broker_ok: bool, api_ok: bool) -> str:
        if state.system_status == 'EMERGENCY_STOP':
            return 'EMERGENCY_STOP'
        if state.realized_pnl <= -abs(settings.max_daily_loss):
            return 'DAILY_LOSS_LIMIT'
        if self._drawdown_guard(state):
            return 'MAX_DRAWDOWN_LIMIT'
        if state.consecutive_losses >= settings.max_consecutive_losses:
            return 'CONSECUTIVE_LOSS_LIMIT'
        if not broker_ok:
            return 'BROKER_DISCONNECTED'
        if not api_ok:
            return 'API_UNSTABLE'
        return 'RUNNING'
