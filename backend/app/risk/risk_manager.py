# backend/app/engine/risk_manager.py

from __future__ import annotations
from datetime import datetime

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

        async def block(reason: str):
            logger.warning(f"RISK_BLOCKED: {reason}")
            await self.state_manager.update(signal=None, signal_meta=None)
            await self.event_bus.publish("RISK_BLOCKED", {"reason": reason})

        async with self.state_manager.lock:

            state   = await self.state_manager.snapshot()
            payload = event.payload or {}

            # ─────────────────────────────────────────
            # 1. Active trade guard
            # ─────────────────────────────────────────
            if state.active_trade:
                await block("active_trade_open")
                return

            # ─────────────────────────────────────────
            # 2. Max trades per day
            # ─────────────────────────────────────────
            if state.trade_count >= settings.max_trades:
                await block(f"max_trades={settings.max_trades}")
                return

            # ─────────────────────────────────────────
            # 3. Daily loss limit
            # ─────────────────────────────────────────
            if state.daily_pnl <= -abs(settings.max_daily_loss):
                await self.state_manager.update(trading_enabled=False)
                await block(f"max_daily_loss=₹{settings.max_daily_loss}")
                return

            # ─────────────────────────────────────────
            # 4. Global trading switch
            # ─────────────────────────────────────────
            if not state.trading_enabled:
                await block("trading_disabled")
                return

            # ─────────────────────────────────────────
            # 5. REAL equity check (includes unrealized PnL)
            # ─────────────────────────────────────────
            unrealized = getattr(state, "unrealized_pnl", 0.0)
            current_equity = settings.capital + state.daily_pnl + unrealized

            if settings.capital > 0:
                equity_pct = current_equity / settings.capital
                if equity_pct < 0.70:
                    await self.state_manager.update(trading_enabled=False)
                    await block(f"capital_guard equity={equity_pct:.1%}")
                    return

            # ─────────────────────────────────────────
            # 6. Position size control
            # ─────────────────────────────────────────
            qty = payload.get("qty", settings.order_qty)
            max_qty = getattr(settings, "max_qty", settings.order_qty * 5)

            if qty > max_qty:
                await block(f"position_size_exceeded qty={qty} max={max_qty}")
                return

            # ─────────────────────────────────────────
            # 7. Time filter (no late entries)
            # ─────────────────────────────────────────
            now = datetime.now().time()
            if now > settings.no_entry_after:
                await block(f"late_entry_after_{settings.no_entry_after}")
                return

            # ─────────────────────────────────────────
            # 8. Price validation
            # ─────────────────────────────────────────
            if payload.get("price") is None or payload.get("price") <= 0:
                await block("invalid_price")
                return

            # ─────────────────────────────────────────
            # 9. Cooldown after loss
            # ─────────────────────────────────────────
            if getattr(state, "cooldown_active", False):
                await block("cooldown_active")
                return

            # ─────────────────────────────────────────
            # 10. Volatility filter (optional)
            # ─────────────────────────────────────────
            min_atr = getattr(settings, "min_atr", 0)
            if min_atr > 0 and payload.get("atr", 0) < min_atr:
                await block("low_volatility")
                return

            # ─────────────────────────────────────────
            # ✅ PASS — APPROVED
            # ─────────────────────────────────────────
            logger.info(
                "RISK_APPROVED signal=%s qty=%s equity=₹%.0f",
                payload.get("signal"),
                qty,
                current_equity
            )

            await self.state_manager.update(signal=None, signal_meta=None)
            await self.event_bus.publish("RISK_APPROVED", payload)