"""
Lords Bot — Risk Manager
Standalone pre-trade gate. Listens to SIGNAL,
runs all risk checks, publishes RISK_APPROVED or RISK_BLOCKED.
This is the ONLY place risk is evaluated — TradingEngine
trusts RISK_APPROVED and does NOT double-check.
"""
from __future__ import annotations

from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("risk_manager")


class RiskManager:

    def __init__(self, event_bus: EventBus, state_manager: StateManager) -> None:
        self.event_bus     = event_bus
        self.state_manager = state_manager

    async def run(self) -> None:
        queue = self.event_bus.subscribe("SIGNAL")
        async for event in self.event_bus.iter_events(queue):
            await self._evaluate(event)

    async def _evaluate(self, event) -> None:
        state   = await self.state_manager.snapshot()
        payload = event.payload

        def block(reason: str):
            logger.info("RISK_BLOCKED: %s", reason)
            return self.event_bus.publish("RISK_BLOCKED", {"reason": reason})

        if state.active_trade:
            await block("active_trade_open"); return

        if state.trade_count >= settings.max_trades:
            await block(f"max_trades={settings.max_trades}"); return

        if state.daily_pnl <= -abs(settings.max_daily_loss):
            await self.state_manager.update(trading_enabled=False)
            await block(f"max_daily_loss=₹{settings.max_daily_loss}"); return

        if not state.trading_enabled:
            await block("trading_disabled"); return

        # Capital guard: stop if equity < 70% of starting capital
        if settings.capital > 0:
            equity_pct = (settings.capital + state.daily_pnl) / settings.capital
            if equity_pct < 0.70:
                await self.state_manager.update(trading_enabled=False)
                await block(f"capital_guard equity={equity_pct:.1%}"); return

        logger.info("RISK_APPROVED signal=%s size=%s", payload.get("signal"), payload.get("size_label"))
        await self.event_bus.publish("RISK_APPROVED", payload)
