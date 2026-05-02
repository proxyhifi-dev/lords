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
    def __init__(self, event_bus: EventBus, state_manager: StateManager, broker: SamcoClient):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.broker = broker

    async def run(self) -> None:
        queue = self.event_bus.subscribe("SIGNAL")
        async for event in self.event_bus.iter_events(queue):
            await self._evaluate(event.payload or {})

    async def _evaluate(self, payload: Dict[str, Any]) -> None:
        state = await self.state_manager.snapshot()
        
        if not state.trading_enabled:
            await self._block("trading_disabled")
            return

        # ✅ FIX: Only require 'signal' — scheduler emits {signal, spot_price, size_label, trend_score}
        if not payload.get("signal"):
            await self._block("signal_missing")
            return

        # Entry time check
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
        """Validate Iron Condor position against all risk limits.
        
        Checks:
        1. Minimum ₹40,000 margin available
        2. Position won't exceed max ₹10,000 loss
        3. No existing active position
        4. Daily loss limits respected
        
        Args:
            net_premium: Net credit collected (entry premium)
            state: RuntimeState snapshot
            
        Returns:
            True if position is safe to place, False otherwise
        """
        
        # Check 1: Minimum ₹40,000 margin required
        margin_required = settings.ic_margin_required  # ₹40,000
        equity_available = self.state_manager.capital - self.state_manager.equity_used
        
        if equity_available < margin_required:
            logger.error(
                f"🚫 INSUFFICIENT IC MARGIN: ₹{equity_available:.0f} < ₹{margin_required:.0f}"
            )
            return False
        
        logger.info(f"✅ Margin check passed: ₹{equity_available:.0f} available")
        
        # Check 2: Entry premium doesn't exceed max loss cap
        # Max loss per trade is capped at ₹10,000
        max_loss_allowed = settings.ic_max_loss_per_trade  # ₹10,000
        
        if net_premium > max_loss_allowed:
            logger.error(
                f"🚫 ENTRY PREMIUM TOO HIGH: ₹{net_premium:.0f} > ₹{max_loss_allowed:.0f} max"
            )
            return False
        
        logger.info(f"✅ Premium check passed: ₹{net_premium:.0f} acceptable")
        
        # Check 3: No active position already exists
        if state.active_trade:
            logger.error(
                f"🚫 POSITION ALREADY ACTIVE: Cannot open IC while {state.active_trade.get('symbol')} is open"
            )
            return False
        
        logger.info("✅ No active position: clear to enter")
        
        # Check 4: Daily loss limits respected
        max_daily_loss = settings.MAX_DAILY_LOSS
        if state.daily_pnl < -max_daily_loss:
            logger.error(
                f"🚫 DAILY LOSS LIMIT EXCEEDED: ₹{state.daily_pnl:.0f} < -₹{max_daily_loss:.0f}"
            )
            return False
        
        logger.info(f"✅ Daily limit check passed: ₹{state.daily_pnl:.0f} / -₹{max_daily_loss:.0f}")
        
        # All checks passed
        logger.info("✅ IC POSITION VALIDATED - SAFE TO PLACE")
        return True
