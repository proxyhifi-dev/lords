from __future__ import annotations

from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.config import settings


class RiskManager:
    def __init__(self, event_bus: EventBus, state_manager: StateManager) -> None:
        self.event_bus = event_bus
        self.state_manager = state_manager

    async def run(self) -> None:
        queue = self.event_bus.subscribe("SIGNAL")
        async for event in self.event_bus.iter_events(queue):
            state = await self.state_manager.snapshot()
            if state.trade_count >= settings.max_trades:
                await self.event_bus.publish("RISK_BLOCKED", {"reason": "max_trades_reached"})
                continue
            if state.daily_pnl <= -abs(settings.max_daily_loss):
                await self.state_manager.update(trading_enabled=False)
                await self.event_bus.publish("RISK_BLOCKED", {"reason": "max_daily_loss"})
                continue
            if not state.trading_enabled:
                await self.event_bus.publish("RISK_BLOCKED", {"reason": "trading_disabled"})
                continue
            await self.event_bus.publish("RISK_APPROVED", event.payload)
