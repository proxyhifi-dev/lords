"""
Lords Bot — Trade Reconciliation Engine  v5.0
==============================================
Runs at startup and every 5 minutes during market hours.
Compares SAMCO positions/tradebook against local state.
Detects: missing trades, qty mismatch, phantom positions.
Auto-corrects or triggers emergency exit.
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

    def __init__(self, broker: SamcoClient, state_manager: StateManager,
                 event_bus=None):
        self.broker        = broker
        self.state_manager = state_manager
        self.event_bus     = event_bus

    async def run_once(self) -> dict:
        """
        Run a full reconciliation check.
        Returns summary dict with any issues found.
        """
        if not settings.is_live:
            logger.debug("Reconciliation skipped — paper mode")
            return {"mode": "paper", "status": "skipped"}

        logger.info("Starting reconciliation check")
        result = {
            "timestamp":       datetime.now(IST).isoformat(),
            "issues_found":    0,
            "actions_taken":   [],
            "status":          "ok",
        }

        try:
            state     = await self.state_manager.snapshot()
            positions = await self.broker.get_positions()
            trades    = await self.broker.get_trade_book()

            # ── Check 1: phantom position ────────────────────
            # SAMCO has open position but bot thinks no trade
            nifty_positions = [
                p for p in positions
                if "NIFTY" in str(p.get("tradingSymbol", "")).upper()
                and str(p.get("netQty") or p.get("netQuantity") or "0") not in ("0", "")
            ]

            if nifty_positions and not state.active_trade:
                result["issues_found"] += 1
                for pos in nifty_positions:
                    sym = pos.get("tradingSymbol", "unknown")
                    qty = pos.get("netQty") or pos.get("netQuantity") or "?"
                    avg = pos.get("averagePrice") or pos.get("avgPrice") or "?"
                    logger.critical(
                        "RECONCILIATION: Phantom position detected — "
                        "SAMCO has %s qty=%s avg=₹%s but bot has no active trade",
                        sym, qty, avg
                    )
                    result["actions_taken"].append(
                        f"PHANTOM_POSITION: {sym} qty={qty} — emergency exit triggered"
                    )
                    # Trigger emergency exit
                    if self.event_bus:
                        await self.event_bus.publish("RECONCILE_PHANTOM_POSITION", {
                            "symbol": sym, "qty": qty, "avg_price": avg
                        })
                    else:
                        # Direct emergency sell
                        await self._emergency_exit(sym, int(str(qty).replace(",", "") or 0))

            # ── Check 2: local trade but no SAMCO position ───
            if state.active_trade and not nifty_positions:
                sym = state.active_trade.get("symbol", "unknown")
                qty = state.active_trade.get("qty", 0)
                logger.warning(
                    "RECONCILIATION: Local trade %s qty=%s but no SAMCO position — "
                    "may have been filled/closed externally",
                    sym, qty
                )
                result["issues_found"] += 1
                result["actions_taken"].append(
                    f"GHOST_TRADE: {sym} — clearing local state (no SAMCO position)"
                )
                await self.state_manager.update(active_trade=None, live_pnl=0.0)

            # ── Check 3: qty mismatch ────────────────────────
            if state.active_trade and nifty_positions:
                local_sym = state.active_trade.get("symbol", "")
                for pos in nifty_positions:
                    samco_sym = pos.get("tradingSymbol", "")
                    if samco_sym != local_sym:
                        continue
                    samco_qty = abs(int(str(
                        pos.get("netQty") or pos.get("netQuantity") or 0
                    ).replace(",", "")))
                    # Account for T1 partial: after T1, local qty = t2_qty
                    expected_qty = (
                        state.active_trade.get("t2_qty", state.active_trade.get("qty", 0))
                        if state.active_trade.get("t1_booked")
                        else state.active_trade.get("qty", 0)
                    )
                    if samco_qty != expected_qty:
                        result["issues_found"] += 1
                        logger.warning(
                            "RECONCILIATION: Qty mismatch %s — local=%s SAMCO=%s",
                            samco_sym, expected_qty, samco_qty
                        )
                        result["actions_taken"].append(
                            f"QTY_MISMATCH: {samco_sym} local={expected_qty} samco={samco_qty}"
                        )

            # ── Check 4: tradebook PnL vs local PnL ─────────
            today_pnl_samco = 0.0
            for t in trades:
                try:
                    pnl = float(str(t.get("pnl") or t.get("profitLoss") or 0).replace(",", ""))
                    today_pnl_samco += pnl
                except (ValueError, TypeError):
                    pass

            if abs(today_pnl_samco - state.daily_pnl) > 500 and today_pnl_samco != 0:
                logger.warning(
                    "RECONCILIATION: P&L mismatch — local=₹%.2f SAMCO=₹%.2f diff=₹%.2f",
                    state.daily_pnl, today_pnl_samco,
                    abs(today_pnl_samco - state.daily_pnl)
                )
                result["issues_found"] += 1
                result["actions_taken"].append(
                    f"PNL_MISMATCH: local=₹{state.daily_pnl:.2f} samco=₹{today_pnl_samco:.2f}"
                )

            if result["issues_found"] == 0:
                logger.info("Reconciliation OK — no issues found")
            else:
                logger.warning("Reconciliation found %d issue(s)", result["issues_found"])
                result["status"] = "issues_found"

        except Exception as exc:
            logger.error("Reconciliation error: %s", exc, exc_info=True)
            result["status"] = "error"
            result["error"]  = str(exc)

        return result

    async def run_loop(self, interval_seconds: int = 300):
        """Run reconciliation every N seconds during market hours."""
        while True:
            await asyncio.sleep(interval_seconds)
            now = datetime.now(IST)
            # Only run during market hours on weekdays
            if now.weekday() < 5 and 9 <= now.hour < 16:
                await self.run_once()

    async def _emergency_exit(self, symbol: str, qty: int):
        """Emergency exit for phantom position."""
        if qty <= 0:
            logger.warning("Emergency exit skipped — qty=0 for %s", symbol)
            return
        logger.critical("EMERGENCY EXIT: selling %s qty=%s", symbol, qty)
        try:
            resp = await self.broker.place_order(
                symbol=symbol, side="SELL", quantity=qty)
            logger.critical("Emergency exit order placed: %s", resp)
        except Exception as exc:
            logger.critical("Emergency exit failed: %s", exc)
