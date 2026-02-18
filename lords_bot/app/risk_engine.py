from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lords_bot.app.pnl_tracker import PnLTracker

risk_logger = logging.getLogger("lords_bot.risk")
trade_logger = logging.getLogger("lords_bot.trade")
IST = ZoneInfo("Asia/Kolkata")


class RiskEngine:
    def __init__(self, client, order_service, trading_mode: str = "PAPER") -> None:
        self.client = client
        self.order_service = order_service
        self.mode = trading_mode.upper()

        paper_capital = float(os.getenv("PAPER_CAPITAL", "100000"))
        live_capital = float(os.getenv("LIVE_CAPITAL", "100000"))
        initial_capital = live_capital if self.mode == "LIVE" else paper_capital

        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.15"))
        self.min_rr_ratio = float(os.getenv("MIN_RR_RATIO", "1.5"))
        self.max_capital_per_trade = float(os.getenv("MAX_CAPITAL_PER_TRADE", "5000"))
        self.max_risk_pct_per_trade = float(os.getenv("MAX_RISK_PCT_PER_TRADE", "1.0")) / 100.0
        self.daily_max_loss = float(os.getenv("DAILY_MAX_LOSS", "2500"))
        self.max_trades_per_day = int(os.getenv("MAX_TRADES_PER_DAY", "3"))
        self.max_consecutive_losses = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2"))
        self.force_square_off_time = dt.time(15, 15)

        self.tracker = PnLTracker(initial_capital=initial_capital)
        self.active_trade: dict[str, Any] | None = None
        self.last_trade_date: dt.date | None = None
        self.monitor_result: dict[str, Any] | None = None
        self._monitor_task: asyncio.Task | None = None

        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_realized_pnl = 0.0
        self.reconciled_positions: list[dict[str, Any]] = []

        self.trade_journal_path = Path(os.getenv("TRADE_JOURNAL_FILE", "logs/trade_journal.jsonl"))
        self.trade_journal_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def current_capital(self) -> float:
        return self.tracker.current_capital

    @property
    def total_pnl(self) -> float:
        return self.tracker.realized_pnl + self.tracker.unrealized_pnl

    async def reconcile_positions_on_startup(self) -> dict[str, Any]:
        try:
            resp = await self.client.request("GET", "/positions")
            positions = resp.get("netPositions") or resp.get("overall") or resp.get("positions") or []
            self.reconciled_positions = [p for p in positions if abs(float(p.get("netQty", p.get("qty", 0)) or 0)) > 0]
            return {"status": "ok", "open_positions": len(self.reconciled_positions)}
        except Exception as exc:
            risk_logger.exception("Position reconciliation failed: %s", exc)
            return {"status": "error", "reason": str(exc), "open_positions": 0}

    def _is_new_day(self) -> bool:
        return self.last_trade_date != dt.datetime.now(tz=IST).date()

    def _refresh_day(self) -> None:
        if self._is_new_day():
            self.reset_daily_state()

    def can_trade_now(self) -> tuple[bool, str | None]:
        now = dt.datetime.now(tz=IST)
        if now.time() < dt.time(9, 15) or now.time() > self.force_square_off_time:
            return False, "outside_intraday_window"

        if self.client.is_trading_paused():
            return False, f"api_circuit_breaker_{self.client.trading_pause_remaining_seconds}s"

        if self.daily_realized_pnl <= -abs(self.daily_max_loss):
            return False, "daily_loss_limit_hit"

        if self.trades_today >= self.max_trades_per_day:
            return False, "max_trades_reached"

        if self.consecutive_losses >= self.max_consecutive_losses:
            return False, "consecutive_loss_limit_hit"

        if self.active_trade:
            return False, "active_trade_exists"

        return True, None

    def reset_daily_state(self) -> None:
        self.last_trade_date = dt.datetime.now(tz=IST).date()
        self.active_trade = None
        self.monitor_result = None
        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_realized_pnl = 0.0
        self.tracker.reset_daily()

    def _compute_quantity(self, entry_price: float) -> tuple[int, float]:
        lot_size = int(os.getenv("OPTION_LOT_SIZE", "75"))
        max_by_capital = int(self.max_capital_per_trade // max(entry_price, 1))
        max_risk_amount = self.current_capital * self.max_risk_pct_per_trade
        per_unit_risk = max(entry_price * self.stop_loss_pct, 0.01)
        max_by_risk = int(max_risk_amount // per_unit_risk)
        raw_qty = max(0, min(max_by_capital, max_by_risk))
        lots = raw_qty // lot_size
        qty = lots * lot_size
        return qty, round(max_risk_amount, 2)

    async def execute_trade(self, signal: dict[str, Any]) -> dict[str, Any]:
        self._refresh_day()
        allowed, reason = self.can_trade_now()
        if not allowed:
            return {"status": "blocked", "reason": reason}

        symbol = signal["symbol"]
        entry_reference = float(signal["ltp"])
        qty, risk_amount = self._compute_quantity(entry_reference)
        if qty <= 0:
            return {"status": "blocked", "reason": "quantity_below_minimum_for_risk"}

        side = 1 if str(signal.get("direction", "CALL")).upper() == "CALL" else -1
        sl_projection, target_projection = self.order_service.build_sl_target(
            entry_reference,
            self.stop_loss_pct,
            self.min_rr_ratio,
        )

        broker_response: dict[str, Any] | None = None
        try:
            if self.mode == "LIVE":
                broker_response = await self.order_service.place_order(
                    {
                        "symbol": symbol,
                        "qty": qty,
                        "type": 2,
                        "side": side,
                        "productType": "INTRADAY",
                        "limitPrice": 0,
                        "stopPrice": 0,
                        "validity": "DAY",
                        "disclosedQty": 0,
                        "offlineOrder": False,
                        "stopLoss": 0,
                        "takeProfit": 0,
                        "orderTag": "ORBENTRY",
                    }
                )
            else:
                broker_response = await self.order_service.simulate_paper_fill(symbol)

            fill_price = await self.order_service.confirm_fill_price(symbol, broker_response)
        except Exception as exc:
            risk_logger.exception("Entry execution failed: %s", exc)
            return {"status": "failed", "reason": str(exc)}

        stop_loss, target = self.order_service.build_sl_target(fill_price, self.stop_loss_pct, self.min_rr_ratio)

        now = dt.datetime.now(tz=IST)
        self.active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry_price": fill_price,
            "stop_loss": stop_loss,
            "target": target,
            "projected_stop_loss": sl_projection,
            "projected_target": target_projection,
            "status": "OPEN",
            "entry_time": now.isoformat(),
            "mode": self.mode,
            "direction": signal.get("direction"),
            "risk_amount": risk_amount,
            "lot_size": int(os.getenv("OPTION_LOT_SIZE", "75")),
            "breakout": signal.get("breakout", {}),
            "broker_response": broker_response,
        }
        self.last_trade_date = now.date()
        self.trades_today += 1
        self.monitor_result = {"status": "monitoring"}

        self._monitor_task = asyncio.create_task(self._monitor_until_exit())
        return {"status": "executed", "trade": self.active_trade}

    async def monitor_trade(self) -> dict[str, Any]:
        self._refresh_day()
        if not self.active_trade:
            return self.monitor_result or {"status": "idle", "pnl": self.tracker.snapshot()}

        try:
            symbol = self.active_trade["symbol"]
            ltp = await self.order_service.fetch_ltp(symbol)
        except Exception as exc:
            risk_logger.exception("Monitor quote fetch failed: %s", exc)
            return {"status": "error", "reason": str(exc), "trade": self.active_trade}

        entry = float(self.active_trade["entry_price"])
        qty = int(self.active_trade["qty"])
        direction = str(self.active_trade.get("direction", "CALL")).upper()
        sign = 1 if direction == "CALL" else -1

        unrealized = round((ltp - entry) * qty * sign, 2)
        self.tracker.update_unrealized(unrealized)

        self.active_trade["last_price"] = ltp
        self.active_trade["unrealized_pnl"] = unrealized

        now_ist = dt.datetime.now(tz=IST)
        if now_ist.time() >= self.force_square_off_time:
            return await self._exit_trade(exit_reason="FORCE_SQUARE_OFF", exit_price=ltp)

        if sign == 1 and ltp <= self.active_trade["stop_loss"]:
            return await self._exit_trade(exit_reason="STOP_LOSS", exit_price=ltp)
        if sign == 1 and ltp >= self.active_trade["target"]:
            return await self._exit_trade(exit_reason="TARGET", exit_price=ltp)
        if sign == -1 and ltp >= self.active_trade["stop_loss"]:
            return await self._exit_trade(exit_reason="STOP_LOSS", exit_price=ltp)
        if sign == -1 and ltp <= self.active_trade["target"]:
            return await self._exit_trade(exit_reason="TARGET", exit_price=ltp)

        return {"status": "open", "trade": self.active_trade, "pnl": self.tracker.snapshot()}

    async def _monitor_until_exit(self) -> None:
        while self.active_trade and self.active_trade.get("status") == "OPEN":
            try:
                result = await self.monitor_trade()
                if result.get("status") in {"closed", "exit_failed"}:
                    break
            except Exception as exc:
                risk_logger.exception("Background monitor failure handled safely: %s", exc)
            await asyncio.sleep(5)

    async def _exit_trade(self, *, exit_reason: str, exit_price: float) -> dict[str, Any]:
        if not self.active_trade:
            return {"status": "idle"}

        exit_response: dict[str, Any] | None = None
        side = -1 if str(self.active_trade.get("direction", "CALL")).upper() == "CALL" else 1

        if self.mode == "LIVE":
            try:
                exit_response = await self.order_service.place_order(
                    {
                        "symbol": self.active_trade["symbol"],
                        "qty": self.active_trade["qty"],
                        "type": 2,
                        "side": side,
                        "productType": "INTRADAY",
                        "limitPrice": 0,
                        "stopPrice": 0,
                        "validity": "DAY",
                        "disclosedQty": 0,
                        "offlineOrder": False,
                        "stopLoss": 0,
                        "takeProfit": 0,
                        "orderTag": "ORBEXIT",
                    }
                )
            except Exception as exc:
                risk_logger.exception("Live exit order failed: %s", exc)
                self.monitor_result = {"status": "exit_failed", "error": str(exc), "trade": self.active_trade}
                return self.monitor_result

        sign = 1 if str(self.active_trade.get("direction", "CALL")).upper() == "CALL" else -1
        pnl = round((exit_price - self.active_trade["entry_price"]) * self.active_trade["qty"] * sign, 2)
        self.tracker.record_realized(pnl)
        self.tracker.update_unrealized(0.0)
        self.daily_realized_pnl += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0

        closed_trade = {
            **self.active_trade,
            "status": "CLOSED",
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "exit_time": dt.datetime.now(tz=IST).isoformat(),
            "realized_pnl": pnl,
            "exit_response": exit_response,
        }
        self._append_trade_journal(closed_trade)

        self.active_trade = None
        self.monitor_result = {
            "status": "closed",
            "trade": closed_trade,
            "pnl": self.tracker.snapshot(),
            "capital": self.current_capital,
        }
        return self.monitor_result

    def _append_trade_journal(self, trade: dict[str, Any]) -> None:
        row = {
            "timestamp": dt.datetime.now(tz=IST).isoformat(),
            "mode": self.mode,
            "trade": trade,
            "daily_realized_pnl": round(self.daily_realized_pnl, 2),
            "trades_today": self.trades_today,
        }
        with self.trade_journal_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, separators=(",", ":")) + "\n")
        trade_logger.info("Trade journal updated for %s", trade.get("symbol"))
