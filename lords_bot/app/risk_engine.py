from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from typing import Any
from zoneinfo import ZoneInfo

from lords_bot.app.pnl_tracker import PnLTracker

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class RiskEngine:
    def __init__(self, client, order_service, trading_mode: str = "PAPER") -> None:
        self.client = client
        self.order_service = order_service
        self.mode = trading_mode.upper()
        self.quantity = int(os.getenv("OPTION_QTY", "75"))
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.25"))
        self.target_pct = float(os.getenv("TARGET_PCT", "0.40"))
        initial_capital = float(os.getenv("INITIAL_CAPITAL", "100000"))

        self.tracker = PnLTracker(initial_capital=initial_capital)
        self.active_trade: dict[str, Any] | None = None
        self.last_trade_date: dt.date | None = None
        self.monitor_result: dict[str, Any] | None = None
        self._monitor_task: asyncio.Task | None = None

    @property
    def current_capital(self) -> float:
        return self.tracker.current_capital

    @property
    def total_pnl(self) -> float:
        return self.tracker.total_pnl

    def can_trade_today(self) -> bool:
        today = dt.datetime.now(tz=IST).date()
        return self.last_trade_date != today

    def reset_daily_state(self) -> None:
        self.last_trade_date = None
        self.active_trade = None
        self.monitor_result = None

    async def execute_trade(self, signal: dict[str, Any]) -> dict[str, Any]:
        if self.active_trade:
            return {"status": "blocked", "reason": "active_trade_exists"}
        if not self.can_trade_today():
            return {"status": "blocked", "reason": "one_trade_per_day"}

        entry_price = float(signal["ltp"])
        stop_loss = round(entry_price * (1 - self.stop_loss_pct), 2)
        target = round(entry_price * (1 + self.target_pct), 2)
        symbol = signal["symbol"]

        broker_response: dict[str, Any] | None = None
        if self.mode == "LIVE":
            broker_response = await self.order_service.place_order(
                {
                    "symbol": symbol,
                    "qty": self.quantity,
                    "type": 2,
                    "side": 1,
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY",
                    "disclosedQty": 0,
                    "offlineOrder": False,
                    "stopLoss": 0,
                    "takeProfit": 0,
                    "orderTag": "ORB_ENTRY",
                }
            )

        now = dt.datetime.now(tz=IST)
        self.active_trade = {
            "symbol": symbol,
            "qty": self.quantity,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "target": target,
            "status": "OPEN",
            "entry_time": now.isoformat(),
            "mode": self.mode,
            "broker_response": broker_response,
        }
        self.last_trade_date = now.date()
        self.monitor_result = {"status": "monitoring"}

        self._monitor_task = asyncio.create_task(self._monitor_until_exit())
        return {"status": "executed", "trade": self.active_trade}

    async def monitor_trade(self) -> dict[str, Any]:
        if not self.active_trade:
            return self.monitor_result or {"status": "idle"}

        symbol = self.active_trade["symbol"]
        quote = await self.client.request("GET", "/quotes", params={"symbols": symbol})
        ltp = float(quote["d"][0]["v"]["lp"])

        self.active_trade["last_price"] = ltp
        self.active_trade["unrealized_pnl"] = round((ltp - self.active_trade["entry_price"]) * self.active_trade["qty"], 2)

        if ltp <= self.active_trade["stop_loss"]:
            return await self._exit_trade(exit_reason="STOP_LOSS", exit_price=ltp)
        if ltp >= self.active_trade["target"]:
            return await self._exit_trade(exit_reason="TARGET", exit_price=ltp)

        return {"status": "open", "trade": self.active_trade}

    async def _monitor_until_exit(self) -> None:
        while self.active_trade and self.active_trade.get("status") == "OPEN":
            try:
                result = await self.monitor_trade()
                if result.get("status") in {"closed", "exit_failed"}:
                    break
            except Exception as exc:
                logger.exception("Error while monitoring active trade: %s", exc)
            await asyncio.sleep(5)

    async def _exit_trade(self, *, exit_reason: str, exit_price: float) -> dict[str, Any]:
        if not self.active_trade:
            return {"status": "idle"}

        exit_response: dict[str, Any] | None = None
        if self.mode == "LIVE":
            try:
                exit_response = await self.order_service.place_order(
                    {
                        "symbol": self.active_trade["symbol"],
                        "qty": self.active_trade["qty"],
                        "type": 2,
                        "side": -1,
                        "productType": "INTRADAY",
                        "limitPrice": 0,
                        "stopPrice": 0,
                        "validity": "DAY",
                        "disclosedQty": 0,
                        "offlineOrder": False,
                        "stopLoss": 0,
                        "takeProfit": 0,
                        "orderTag": f"ORB_EXIT_{exit_reason}",
                    }
                )
            except Exception as exc:
                logger.exception("Live exit order failed: %s", exc)
                self.monitor_result = {"status": "exit_failed", "error": str(exc), "trade": self.active_trade}
                return self.monitor_result

        pnl = round((exit_price - self.active_trade["entry_price"]) * self.active_trade["qty"], 2)
        self.tracker.record_trade(pnl)

        closed_trade = {
            **self.active_trade,
            "status": "CLOSED",
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "exit_time": dt.datetime.now(tz=IST).isoformat(),
            "realized_pnl": pnl,
            "exit_response": exit_response,
        }

        self.active_trade = None
        self.monitor_result = {
            "status": "closed",
            "trade": closed_trade,
            "total_pnl": self.total_pnl,
            "capital": self.current_capital,
        }
        return self.monitor_result
