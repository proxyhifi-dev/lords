from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from lords_bot.app.config import get_settings
from lords_bot.app.fyers_client import FyersAPIError
from lords_bot.app.pnl_tracker import PnLTracker

risk_logger = logging.getLogger("lords_bot.risk")
IST = ZoneInfo("Asia/Kolkata")


class RiskEngine:
    """Risk gatekeeper: validates whether a signal can become a live order."""

    def __init__(self, client, order_service, trading_mode: str = "PAPER") -> None:
        self.settings = get_settings()
        self.client = client
        self.order_service = order_service
        self.mode = trading_mode.upper()

        self.initial_capital = float(self.settings.initial_capital)
        self.max_daily_loss = abs(float(getattr(self.settings, "daily_max_loss", 2500)))
        self.max_trades_per_day = int(getattr(self.settings, "max_trades_per_day", 3))
        self.risk_per_trade_pct = max(0.001, float(getattr(self.settings, "max_risk_pct_per_trade", 1.0)) / 100.0)

        self.tracker = PnLTracker(initial_capital=self.initial_capital)
        self.active_trade: dict[str, Any] | None = None
        self.monitor_result: dict[str, Any] | None = None

        self.trades_today = 0
        self.daily_realized_pnl = 0.0
        self.shutdown_triggered = False
        self.last_trade_date: dt.date | None = None

    @property
    def current_capital(self) -> float:
        return self.tracker.current_capital

    @property
    def total_pnl(self) -> float:
        return self.tracker.realized_pnl + self.tracker.unrealized_pnl

    def _refresh_day(self) -> None:
        today = dt.datetime.now(tz=IST).date()
        if self.last_trade_date != today:
            self.last_trade_date = today
            self.trades_today = 0
            self.daily_realized_pnl = 0.0
            self.shutdown_triggered = False
            self.tracker.reset_daily()

    async def reconcile_positions_on_startup(self) -> dict[str, Any]:
        """Never block startup: reconciliation failures return warning payload instead of raising."""
        try:
            resp = await self.client.request("GET", "/positions")
            positions = resp.get("netPositions") or resp.get("positions") or []
            open_positions = [p for p in positions if abs(float(p.get("netQty", p.get("qty", 0)) or 0)) > 0]
            risk_logger.info("Reconciled %s open positions.", len(open_positions))
            return {"status": "ok", "open_positions": len(open_positions), "positions": open_positions}
        except FyersAPIError as exc:
            if exc.status_code == 401:
                risk_logger.warning("Startup reconciliation skipped: unauthorized (401).")
                return {"status": "warning", "reason": "unauthorized", "open_positions": 0, "positions": []}
            risk_logger.warning("Startup reconciliation degraded: %s", exc)
            return {"status": "warning", "reason": str(exc), "open_positions": 0, "positions": []}
        except Exception as exc:  # noqa: BLE001
            risk_logger.warning("Startup reconciliation degraded: %s", exc)
            return {"status": "warning", "reason": str(exc), "open_positions": 0, "positions": []}

    def can_trade_now(self) -> tuple[bool, str | None]:
        self._refresh_day()
        if self.shutdown_triggered:
            return False, "risk_shutdown_active"
        if self.daily_realized_pnl <= -self.max_daily_loss:
            self.shutdown_triggered = True
            return False, "max_daily_loss_hit"
        if self.trades_today >= self.max_trades_per_day:
            return False, "max_trades_per_day_hit"
        if self.active_trade:
            return False, "trade_already_open"
        if self.client.is_trading_paused():
            return False, "trading_paused_by_circuit_breaker"
        return True, None

    def compute_qty(self, entry: float) -> int:
        max_risk_amt = self.current_capital * self.risk_per_trade_pct
        sl_distance = max(entry * float(self.settings.stop_loss_pct), 0.1)
        qty = int(max_risk_amt // sl_distance)
        return max(qty, 0)

    async def execute_trade(self, signal: dict[str, Any]) -> dict[str, Any]:
        allowed, reason = self.can_trade_now()
        if not allowed:
            return {"status": "blocked", "reason": reason}

        qty = self.compute_qty(float(signal["ltp"]))
        if qty <= 0:
            return {"status": "blocked", "reason": "quantity_zero_after_risk_calc"}

        order_payload = {
            "symbol": signal["symbol"],
            "qty": qty,
            "type": 2,
            "side": 1 if signal.get("direction") == "CALL" else -1,
            "productType": "INTRADAY",
            "validity": "DAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
        }
        try:
            order_response = await self.order_service.place_order(order_payload) if self.mode == "LIVE" else {
                "status": "simulated"
            }
            fill = await self.order_service.confirm_fill_price(signal["symbol"], order_response)
        except Exception as exc:  # noqa: BLE001
            risk_logger.exception("Execution failed safely: %s", exc)
            return {"status": "failed", "reason": str(exc)}

        stop_loss = round(fill * (1 - float(self.settings.stop_loss_pct)), 2)
        target = round(fill * (1 + float(self.settings.target_pct)), 2)
        self.active_trade = {
            "symbol": signal["symbol"],
            "direction": signal.get("direction"),
            "qty": qty,
            "entry_price": fill,
            "stop_loss": stop_loss,
            "target": target,
            "status": "OPEN",
        }
        self.trades_today += 1
        self.monitor_result = {"status": "open", "trade": self.active_trade}
        return {"status": "executed", "trade": self.active_trade}

    async def monitor_trade(self) -> dict[str, Any]:
        if not self.active_trade:
            return {"status": "idle", "capital": self.current_capital, "pnl": self.total_pnl}

        ltp = await self.order_service.fetch_ltp(self.active_trade["symbol"])
        sign = 1 if self.active_trade["direction"] == "CALL" else -1
        pnl = round((ltp - self.active_trade["entry_price"]) * self.active_trade["qty"] * sign, 2)
        self.tracker.update_unrealized(pnl)

        if (sign == 1 and (ltp <= self.active_trade["stop_loss"] or ltp >= self.active_trade["target"])) or (
            sign == -1 and (ltp >= self.active_trade["stop_loss"] or ltp <= self.active_trade["target"])
        ):
            return await self._close_trade(ltp)

        return {"status": "open", "trade": self.active_trade, "ltp": ltp, "pnl": self.total_pnl}

    async def _close_trade(self, exit_price: float) -> dict[str, Any]:
        trade = self.active_trade or {}
        sign = 1 if trade.get("direction") == "CALL" else -1
        realized = round((exit_price - trade.get("entry_price", 0.0)) * trade.get("qty", 0) * sign, 2)
        self.tracker.record_realized(realized)
        self.tracker.update_unrealized(0.0)
        self.daily_realized_pnl += realized
        trade["status"] = "CLOSED"
        trade["exit_price"] = exit_price
        self.active_trade = None
        if self.daily_realized_pnl <= -self.max_daily_loss:
            self.shutdown_triggered = True
            risk_logger.error("Risk shutdown activated after daily loss breach.")
        return {"status": "closed", "trade": trade, "capital": self.current_capital, "pnl": self.total_pnl}

    async def square_off_and_shutdown(self) -> None:
        """Graceful shutdown hook used by main.py to avoid dangling exposure."""
        if self.active_trade:
            with contextlib.suppress(Exception):
                ltp = await self.order_service.fetch_ltp(self.active_trade["symbol"])
                await self._close_trade(ltp)
        self.shutdown_triggered = True
        await asyncio.sleep(0)
