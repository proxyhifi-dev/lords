from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.utils.logger import get_logger, log_event

settings = get_settings()
logger = get_logger("risk_manager")
IST = ZoneInfo("Asia/Kolkata")


class RiskManager:
    def __init__(
        self,
        event_bus: EventBus,
        state_manager: StateManager,
        broker: SamcoClient | None = None,
    ):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.broker = broker

    def _is_paper_mode(self) -> bool:
        return str(getattr(settings, "mode", "paper")).strip().lower() == "paper"

    def _paper_mode_use_broker(self) -> bool:
        return bool(getattr(settings, "paper_mode_use_broker", False))

    def _is_live_mode(self) -> bool:
        if hasattr(settings, "is_live"):
            try:
                return bool(settings.is_live)
            except Exception:
                pass
        return str(getattr(settings, "mode", "paper")).strip().lower() == "live"

    def _strict_live_checks_enabled(self) -> bool:
        """
        Live-only checks should run only when this RiskManager has a real broker.

        This keeps unit tests/paper-only validation from failing when .env has
        MODE=live, while real live runtime still fails closed.
        """
        return self._is_live_mode() and self.broker is not None

    async def run(self) -> None:
        logger.info("RiskManager listening for SIGNAL events")
        queue = self.event_bus.subscribe("SIGNAL")

        async for event in self.event_bus.iter_events(queue):
            logger.info("RiskManager received SIGNAL payload=%s", event.payload)
            await self._evaluate(event.payload or {})

    async def _evaluate(self, payload: Dict[str, Any] | Any) -> None:
        if hasattr(payload, "payload"):
            payload = payload.payload or {}

        state = await self.state_manager.snapshot()

        logger.info(
            "RiskManager evaluating payload=%s trading_enabled=%s active_trade=%s trade_count=%s daily_pnl=%s",
            payload,
            getattr(state, "trading_enabled", None),
            bool(getattr(state, "active_trade", None)),
            getattr(state, "trade_count", None),
            getattr(state, "daily_pnl", None),
        )

        if not state.trading_enabled:
            await self._block("trading_disabled")
            return

        signal = payload.get("signal")
        if not signal:
            await self._block("signal_missing")
            return

        if state.active_trade is not None:
            await self._block("active_trade_exists")
            return

        # Real live runtime must have spot before approval.
        # Unit tests may instantiate RiskManager without broker, so do not apply
        # live broker gates unless a broker exists.
        if (
            getattr(state, "spot_price", None) is None
            and self._strict_live_checks_enabled()
        ):
            await self._block("spot_price_missing")
            return

        if self._strict_live_checks_enabled():
            now = datetime.now(IST).time()

            try:
                h, m = map(int, str(settings.no_entry_after).split(":")[:2])
                cutoff = time(h, m)
            except Exception:
                cutoff = time(15, 0)
                logger.error(
                    "Invalid NO_ENTRY_AFTER=%r; using fallback cutoff=%s",
                    getattr(settings, "no_entry_after", None),
                    cutoff.isoformat(timespec="minutes"),
                )

            if now > cutoff:
                await self._block(
                    "late_entry",
                    {
                        "now": now.isoformat(timespec="minutes"),
                        "cutoff": cutoff.isoformat(timespec="minutes"),
                    },
                )
                return

        if state.daily_pnl <= -settings.max_daily_loss:
            await self._block("max_daily_loss_hit", {"daily_pnl": state.daily_pnl})
            logger.critical("MAX_DAILY_LOSS hit: ₹%.2f", state.daily_pnl)

            await self.state_manager.update(
                trading_enabled=False,
                last_risk_breach="max_daily_loss",
            )
            return

        if state.trade_count >= settings.max_trades:
            await self._block("max_trades_hit", {"trade_count": state.trade_count})
            return

        logger.info(
            "RISK_APPROVED signal=%s size=%s spot=%s",
            payload.get("signal"),
            payload.get("size_label"),
            getattr(state, "spot_price", None),
        )

        log_event("RISK_APPROVED", **payload)

        await self.state_manager.update(signal=None, signal_meta=None)
        await self.event_bus.publish("RISK_APPROVED", payload)

    async def _critical_fail_closed(self, reason: str) -> None:
        logger.critical("CRITICAL_FAIL_CLOSED: %s", reason)

        await self.state_manager.update(
            trading_enabled=False,
            last_order_failed=True,
            last_risk_breach=reason,
        )

        if self.broker is not None and not self._is_paper_mode():
            try:
                await self.broker.cancel_all_open_orders()
                await self.broker.close_all_positions_market()
            except Exception as exc:
                logger.critical("critical shutdown failed: %s", exc)
        else:
            logger.warning(
                "Paper mode or broker unavailable: skipping live close-out during critical fail reason=%s",
                reason,
            )

        await self.event_bus.publish(
            "RISK_BLOCKED",
            {
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def _block(self, reason: str, details: Dict[str, Any] | None = None) -> None:
        logger.warning("RISK_BLOCKED reason=%s details=%s", reason, details)

        await self.state_manager.update(signal=None, signal_meta=None)

        await self.event_bus.publish(
            "RISK_BLOCKED",
            {
                "reason": reason,
                "details": details,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def _extract_volume(quote: Dict[str, Any]) -> int:
        for key in ("tradedVolume", "volume", "totalTradedVolume"):
            val = quote.get(key)

            if val is not None:
                try:
                    volume = int(float(str(val).replace(",", "")))
                    if volume > 0:
                        return volume
                except Exception:
                    pass

        return 0

    async def validate_iron_condor_position(self, net_premium: float, state) -> bool:
        logger.info(
            "Validating Iron Condor position net_premium=%.2f capital=%.2f daily_pnl=%.2f active_trade=%s",
            net_premium,
            float(getattr(settings, "capital", 0)),
            float(getattr(state, "daily_pnl", 0)),
            bool(getattr(state, "active_trade", None)),
        )

        margin_required = float(getattr(settings, "ic_margin_required", 0))
        current_equity = (
            float(getattr(settings, "capital", 0))
            + float(getattr(state, "daily_pnl", 0))
        )

        if current_equity < margin_required:
            logger.error(
                "INSUFFICIENT IC MARGIN: ₹%.0f available < ₹%.0f required",
                current_equity,
                margin_required,
            )
            return False

        logger.info("Margin check passed: ₹%.0f available", current_equity)

        max_loss_allowed = float(getattr(settings, "ic_max_loss_per_trade", 0))

        # For an Iron Condor the net_premium is the credit RECEIVED (higher = better).
        # The potential maximum loss = (spread_width − net_premium) × qty.
        # Block entry only when that worst-case loss would exceed the per-trade cap.
        spread_width = float(getattr(settings, "ic_wing_width", 100))
        order_qty = float(getattr(settings, "order_qty", 65))
        max_potential_loss = max(0.0, (spread_width - net_premium) * order_qty)

        if max_loss_allowed > 0 and max_potential_loss > max_loss_allowed:
            logger.error(
                "IC MAX LOSS EXCEEDED: potential_loss=₹%.0f > allowed=₹%.0f (spread_width=%.0f net_premium=%.2f qty=%.0f)",
                max_potential_loss,
                max_loss_allowed,
                spread_width,
                net_premium,
                order_qty,
            )
            return False

        logger.info(
            "Premium check passed: net_premium=₹%.2f potential_loss=₹%.0f allowed=₹%.0f",
            net_premium,
            max_potential_loss,
            max_loss_allowed,
        )

        if state.active_trade:
            logger.error("POSITION ALREADY ACTIVE — cannot open IC")
            return False

        logger.info("No active position: clear to enter")

        max_daily_loss = float(getattr(settings, "max_daily_loss", 0))

        if state.daily_pnl <= -max_daily_loss:
            logger.error(
                "DAILY LOSS LIMIT EXCEEDED: ₹%.0f <= -₹%.0f",
                state.daily_pnl,
                max_daily_loss,
            )
            return False

        logger.info(
            "Daily limit check passed: ₹%.0f / -₹%.0f",
            state.daily_pnl,
            max_daily_loss,
        )

        max_trades = int(getattr(settings, "max_trades", 1))
        trade_count = int(getattr(state, "trade_count", 0))

        if trade_count >= max_trades:
            logger.error(
                "MAX TRADES REACHED: %d >= %d",
                trade_count,
                max_trades,
            )
            return False

        logger.info("Trade count check passed: %d / %d", trade_count, max_trades)
        logger.info("IC POSITION VALIDATED — SAFE TO PLACE")

        return True
