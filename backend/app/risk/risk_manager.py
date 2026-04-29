from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.utils.logger import get_logger

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

        required = ["signal", "symbol", "qty", "stop_loss_price"]
        missing = [f for f in required if payload.get(f) is None]
        if missing:
            await self._block("required_fields_missing", {"missing": missing})
            return

        qty = int(payload.get("qty", 0))
        if qty <= 0:
            await self._block("invalid_qty")
            return

        try:
            quote = await self.broker.get_quote(payload["symbol"], exchange=payload.get("exchange", "NFO"))
            price = self.broker.parse_ltp(quote)
            bid, ask = self.broker.parse_bid_ask(quote)
        except Exception as exc:
            await self._critical_fail_closed(f"broker_quote_failure:{exc}")
            return

        if price is None or price <= 0:
            await self._block("invalid_price")
            return

        volume = self._extract_volume(quote)
        if volume <= 0:
            await self._block("volume_missing_or_zero")
            return

        if bid and ask and ask > 0 and ask >= bid:
            spread_pct = (ask - bid) / ask
            if spread_pct > float(getattr(settings, "max_spread_pct", 0.05)):
                await self._block("spread_too_high", {"spread_pct": spread_pct})
                return

        lot_size = int(payload.get("lot_size") or 1)
        stop_loss_price = float(payload["stop_loss_price"])
        notional = price * qty * lot_size
        stop_loss_distance = abs(price - stop_loss_price)
        worst_case_loss = stop_loss_distance * qty * lot_size

        equity = settings.capital + float(getattr(state, "realized_pnl", 0.0)) + float(getattr(state, "unrealized_pnl", 0.0))
        if equity <= 0:
            await self._critical_fail_closed("equity_non_positive")
            return

        max_trade_risk_pct = float(getattr(settings, "max_trade_risk_pct", 0.02))
        if worst_case_loss / equity > max_trade_risk_pct:
            await self._block("max_trade_risk_pct_exceeded", {"worst_case_loss": worst_case_loss, "equity": equity})
            return

        max_portfolio_exposure_pct = float(getattr(settings, "max_portfolio_exposure_pct", 0.50))
        current_exposure = sum((state.positions or {}).values())
        if (current_exposure + notional) / equity > max_portfolio_exposure_pct:
            await self._block("max_portfolio_exposure_pct_exceeded")
            return

        now = datetime.now().time()
        h, m = map(int, str(settings.no_entry_after).split(":"))
        if now > time(h, m):
            await self._block("late_entry")
            return

        payload["computed_notional"] = notional
        payload["computed_worst_case_loss"] = worst_case_loss
        payload["computed_entry_price"] = price
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
