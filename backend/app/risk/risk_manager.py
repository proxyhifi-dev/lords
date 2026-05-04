from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.utils.logger import get_logger, log_event

settings = get_settings()
logger = get_logger("risk_manager")


class RiskManager:
    def __init__(self, event_bus: EventBus, state_manager: StateManager, broker: SamcoClient | None = None):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.broker = broker

    async def run(self) -> None:
        queue = self.event_bus.subscribe("SIGNAL")
        async for event in self.event_bus.iter_events(queue):
            await self._evaluate(event.payload or {})

    async def _evaluate(self, payload: Dict[str, Any] | Any) -> None:
        if hasattr(payload, "payload"):
            payload = payload.payload or {}
        state = await self.state_manager.snapshot()
        
        if not state.trading_enabled:
            await self._block("trading_disabled")
            return

        # ✅ FIX: Only require 'signal' — scheduler emits {signal, spot_price, size_label, trend_score}
        if not payload.get("signal"):
            await self._block("signal_missing")
            return

        if state.active_trade is not None:
            await self._block("active_trade_exists")
            return

        # Entry time check
        if settings.is_live:
            now = datetime.now().time()
            h, m = map(int, str(settings.no_entry_after).split(":"))
            if now > time(h, m):
                await self._block("late_entry")
                return

        # Daily loss check (enforce here, not just at exit)
        if state.daily_pnl <= -settings.max_daily_loss:
            await self._block("max_daily_loss_hit", {"daily_pnl": state.daily_pnl})
            logger.critical("🚨 MAX_DAILY_LOSS hit: ₹%.2f", state.daily_pnl)
            await self.state_manager.update(trading_enabled=False, last_risk_breach="max_daily_loss")
            return

        # Max trades check
        if state.trade_count >= settings.max_trades:
            await self._block("max_trades_hit", {"trade_count": state.trade_count})
            return

        # Pass through to trading_engine (it will do strike/symbol/qty resolution)
        logger.info("RISK_APPROVED signal=%s size=%s", 
                   payload.get("signal"), payload.get("size_label"))
        
        log_event("RISK_APPROVED", **payload)
        
        await self.state_manager.update(signal=None, signal_meta=None)
        await self.event_bus.publish("RISK_APPROVED", payload)

    async def _critical_fail_closed(self, reason: str) -> None:
        logger.critical("CRITICAL_FAIL_CLOSED: %s", reason)
        await self.state_manager.update(trading_enabled=False, last_order_failed=True, last_risk_breach=reason)
        try:
            await self.broker.cancel_all_open_orders()
            await self.broker.close_all_positions_market()
        except Exception as exc:
            logger.critical("critical shutdown failed: %s", exc)
        await self.event_bus.publish("RISK_BLOCKED", {"reason": reason, "timestamp": datetime.now().isoformat()})

    async def _block(self, reason: str, details: Dict[str, Any] | None = None) -> None:
        logger.debug("RISK_BLOCKED reason=%s", reason)
        await self.state_manager.update(signal=None, signal_meta=None)
        await self.event_bus.publish("RISK_BLOCKED", {"reason": reason, "details": details, "timestamp": datetime.now().isoformat()})

    @staticmethod
    def _extract_volume(quote: Dict[str, Any]) -> int:
        for key in ("tradedVolume", "volume", "totalTradedVolume"):
            val = quote.get(key)
            if val is not None:
                try:
                    v = int(float(str(val).replace(",", "")))
                    if v > 0:
                        return v
                except Exception:
                    pass
        return 0

    async def validate_iron_condor_position(self, net_premium: float, state) -> bool:
        """Validate Iron Condor position against all risk limits."""

        # Check 1: Minimum margin available
        # FIX: StateManager has no equity_used attribute.
        # Use settings.capital + state.daily_pnl as current available equity.
        margin_required = settings.ic_margin_required
        current_equity = settings.capital + state.daily_pnl
        if current_equity < margin_required:
            logger.error(
                "🚫 INSUFFICIENT IC MARGIN: ₹%.0f available < ₹%.0f required",
                current_equity, margin_required,
            )
            return False
        logger.info("✅ Margin check passed: ₹%.0f available", current_equity)

        # Check 2: Entry premium vs max loss cap
        max_loss_allowed = settings.ic_max_loss_per_trade
        if net_premium > max_loss_allowed:
            logger.error(
                "🚫 ENTRY PREMIUM TOO HIGH: ₹%.0f > ₹%.0f max",
                net_premium, max_loss_allowed,
            )
            return False
        logger.info("✅ Premium check passed: ₹%.0f acceptable", net_premium)

        # Check 3: No active position
        if state.active_trade:
            logger.error("🚫 POSITION ALREADY ACTIVE — cannot open IC")
            return False
        logger.info("✅ No active position: clear to enter")

        # Check 4: Daily loss limits
        # FIX: settings.max_daily_loss (lowercase), not settings.MAX_DAILY_LOSS
        max_daily_loss = settings.max_daily_loss
        if state.daily_pnl < -max_daily_loss:
            logger.error(
                "🚫 DAILY LOSS LIMIT EXCEEDED: ₹%.0f < -₹%.0f",
                state.daily_pnl, max_daily_loss,
            )
            return False
        logger.info(
            "✅ Daily limit check passed: ₹%.0f / -₹%.0f",
            state.daily_pnl, max_daily_loss,
        )

        logger.info("✅ IC POSITION VALIDATED — SAFE TO PLACE")
        return True
