"""
Lords Bot — Trade Reconciliation Engine  v5.1
==============================================
Runs at startup AND every 5 minutes during market hours (live mode only).
Compares SAMCO positions + tradebook against local bot state.

Detects and handles:
  1. Phantom position — SAMCO has open trade, bot thinks nothing open
     → triggers emergency exit
  2. Ghost trade — bot thinks trade open, SAMCO has no position
     → clears local state
  3. Qty mismatch — SAMCO qty differs from what bot recorded
     → logs warning, does not auto-correct (manual review needed)
  4. P&L drift > ₹500 — SAMCO daily PnL differs from local
     → logs warning for manual review

Wire-up: market_scheduler.py creates this and adds run_loop(300) as a task.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.engine.state_manager import StateManager
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("reconciliation")
IST = ZoneInfo("Asia/Kolkata")


class ReconciliationEngine:

    def __init__(
        self,
        broker: SamcoClient,
        state_manager: StateManager,
        event_bus=None,
    ):
        self.broker        = broker
        self.state_manager = state_manager
        self.event_bus     = event_bus

    # ── Public API ───────────────────────────────────────────

    async def run_once(self) -> dict:
        """
        Single reconciliation pass.
        Returns a summary dict with any issues found + actions taken.
        Call at startup and after any reconnect.
        """
        if not settings.is_live:
            logger.debug("Reconciliation skipped — paper mode")
            return {"mode": "paper", "status": "skipped"}

        logger.info("Reconciliation check starting")
        result: dict = {
            "timestamp":     datetime.now(IST).isoformat(),
            "issues_found":  0,
            "actions_taken": [],
            "status":        "ok",
        }

        try:
            state     = await self.state_manager.snapshot()
            positions = await self.broker.get_positions()
            trades    = await self.broker.get_trade_book()

            # Filter to NIFTY option positions with non-zero net qty
            nifty_pos = [
                p for p in positions
                if "NIFTY" in str(p.get("tradingSymbol", "")).upper()
                and self._net_qty(p) != 0
            ]

            # ── 1. Phantom position ───────────────────────
            # SAMCO has open NIFTY position, bot has no active trade
            if nifty_pos and not state.active_trade:
                result["issues_found"] += 1
                for pos in nifty_pos:
                    sym = pos.get("tradingSymbol", "unknown")
                    qty = self._net_qty(pos)
                    avg = pos.get("averagePrice") or pos.get("avgPrice") or "?"
                    msg = (
                        f"PHANTOM_POSITION: {sym} qty={qty} avg=₹{avg} "
                        f"— bot has no active trade. Emergency exit triggered."
                    )
                    logger.critical("RECONCILE: %s", msg)
                    result["actions_taken"].append(msg)
                    await self._emergency_exit(sym, abs(qty))

            # ── 2. Ghost trade ───────────────────────────
            # Bot thinks trade is open, SAMCO has no matching position
            if state.active_trade and not nifty_pos:
                sym = state.active_trade.get("symbol", "unknown")
                qty = state.active_trade.get("qty", 0)
                msg = (
                    f"GHOST_TRADE: {sym} qty={qty} "
                    f"— local state shows open trade but no SAMCO position. "
                    f"Clearing local state."
                )
                logger.warning("RECONCILE: %s", msg)
                result["issues_found"] += 1
                result["actions_taken"].append(msg)
                await self.state_manager.update(
                    active_trade=None, live_pnl=0.0)

            # ── 3. Qty mismatch ──────────────────────────
            if state.active_trade and nifty_pos:
                local_sym = state.active_trade.get("symbol", "")
                for pos in nifty_pos:
                    if pos.get("tradingSymbol") != local_sym:
                        continue
                    samco_qty = abs(self._net_qty(pos))
                    expected_qty = (
                        state.active_trade.get("t2_qty",
                            state.active_trade.get("qty", 0) // 2)
                        if state.active_trade.get("t1_booked")
                        else state.active_trade.get("qty", 0)
                    )
                    if samco_qty != expected_qty:
                        msg = (
                            f"QTY_MISMATCH: {local_sym} "
                            f"bot_expected={expected_qty} samco_actual={samco_qty} "
                            f"— manual review needed"
                        )
                        logger.warning("RECONCILE: %s", msg)
                        result["issues_found"] += 1
                        result["actions_taken"].append(msg)

            # ── 4. P&L drift ─────────────────────────────
            samco_pnl = self._sum_tradebook_pnl(trades)
            if samco_pnl != 0 and abs(samco_pnl - state.daily_pnl) > 500:
                msg = (
                    f"PNL_MISMATCH: bot=₹{state.daily_pnl:.2f} "
                    f"samco=₹{samco_pnl:.2f} "
                    f"diff=₹{abs(samco_pnl - state.daily_pnl):.2f} "
                    f"— manual review needed"
                )
                logger.warning("RECONCILE: %s", msg)
                result["issues_found"] += 1
                result["actions_taken"].append(msg)

            if result["issues_found"] == 0:
                logger.info("Reconciliation OK — no issues")
            else:
                logger.warning(
                    "Reconciliation found %d issue(s)", result["issues_found"])
                result["status"] = "issues_found"

        except Exception as exc:
            logger.error("Reconciliation error: %s", exc, exc_info=True)
            result["status"] = "error"
            result["error"]  = str(exc)

        return result

    async def run_loop(self, interval_seconds: int = 300) -> None:
        """
        Periodic reconciliation loop.
        Runs every `interval_seconds` (default 5 minutes) during market hours.
        Called as a background task from market_scheduler.start().
        """
        logger.info(
            "Reconciliation loop started — interval=%ds (live mode only)",
            interval_seconds)
        while True:
            await asyncio.sleep(interval_seconds)
            now = datetime.now(IST)
            # Only run on weekdays during market hours
            if now.weekday() < 5 and 9 <= now.hour < 16:
                try:
                    await self.run_once()
                except Exception as exc:
                    logger.error("Reconciliation loop error: %s", exc)

    # ── Private helpers ──────────────────────────────────────

    async def _emergency_exit(self, symbol: str, qty: int) -> None:
        """Place emergency market SELL for a phantom position."""
        if qty <= 0:
            logger.warning(
                "Emergency exit skipped — qty=%d for %s", qty, symbol)
            return
        logger.critical(
            "EMERGENCY EXIT: selling phantom position %s qty=%d", symbol, qty)
        try:
            resp = await self.broker.place_order(
                symbol=symbol, side="SELL", quantity=qty)
            oid = resp.get("orderNumber") or resp.get("orderId")
            logger.critical(
                "Emergency exit order placed order=%s resp=%s", oid, resp)
            if self.event_bus:
                await self.event_bus.publish("RECONCILE_EMERGENCY_EXIT", {
                    "symbol": symbol, "qty": qty, "order_id": oid,
                })
        except Exception as exc:
            logger.critical(
                "Emergency exit failed for %s qty=%d: %s", symbol, qty, exc)

    @staticmethod
    def _net_qty(position: dict) -> int:
        """Extract net qty from a SAMCO position dict."""
        for key in ("netQty", "netQuantity", "net_qty"):
            val = position.get(key)
            if val is not None:
                try:
                    return int(float(str(val).replace(",", "").strip()))
                except (ValueError, TypeError):
                    pass
        return 0

    @staticmethod
    def _sum_tradebook_pnl(trades: list[dict]) -> float:
        """Sum P&L from SAMCO trade book entries."""
        total = 0.0
        for t in trades:
            for key in ("pnl", "profitLoss", "realizedPnl", "profit_loss"):
                val = t.get(key)
                if val is not None:
                    try:
                        total += float(
                            str(val).replace(",", "").replace("₹", "").strip())
                        break
                    except (ValueError, TypeError):
                        pass
        return round(total, 2)
