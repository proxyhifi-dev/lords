"""
Lords Bot — Trade Reconciliation Engine  v5.1
==============================================
Runs at startup AND every 5 minutes during market hours.
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
            # CRITICAL: Force sync state from broker
            if nifty_pos and not state.active_trade:
                result["issues_found"] += 1
                for pos in nifty_pos:
                    sym = pos.get("tradingSymbol", "unknown")
                    qty = self._net_qty(pos)
                    avg = pos.get("averagePrice") or pos.get("avgPrice") or "?"
                    msg = (
                        f"PHANTOM_POSITION: {sym} qty={qty} avg=₹{avg} "
                        f"— bot has no active trade. FORCE SYNC from broker."
                    )
                    logger.critical("RECONCILE: %s", msg)
                    result["actions_taken"].append(msg)

                    # AUTHORITATIVE ACTION: Rebuild state from broker
                    await self._force_sync_from_broker(positions)

            # ── 2. Ghost trade ───────────────────────────
            # Bot thinks trade is open, SAMCO has no matching position
            # CRITICAL: Force clear local state
            if state.active_trade and not nifty_pos:
                sym = state.active_trade.get("symbol", "unknown")
                qty = state.active_trade.get("qty", 0)
                msg = (
                    f"GHOST_TRADE: {sym} qty={qty} "
                    f"— local state shows open trade but no SAMCO position. "
                    f"FORCE CLEAR local state."
                )
                logger.warning("RECONCILE: %s", msg)
                result["issues_found"] += 1
                result["actions_taken"].append(msg)

                # AUTHORITATIVE ACTION: Clear inconsistent state
                await self.state_manager.update(
                    active_trade=None,
                    live_pnl=0.0,
                    unrealized_pnl=0.0,
                    positions={}
                )

            # ── 3. Qty mismatch ──────────────────────────
            # CRITICAL: Force sync quantities from broker
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
                            f"— FORCE SYNC from broker"
                        )
                        logger.warning("RECONCILE: %s", msg)
                        result["issues_found"] += 1
                        result["actions_taken"].append(msg)

                        # AUTHORITATIVE ACTION: Update quantities from broker
                        await self._force_sync_from_broker(positions)

            # ── 4. P&L drift ─────────────────────────────
            # CRITICAL: Force sync P&L from broker
            samco_pnl = self._sum_tradebook_pnl(trades)
            if samco_pnl != 0 and abs(samco_pnl - state.daily_pnl) > 500:
                msg = (
                    f"PNL_MISMATCH: bot=₹{state.daily_pnl:.2f} "
                    f"samco=₹{state.daily_pnl:.2f} "
                    f"samco=₹{samco_pnl:.2f} "
                    f"diff=₹{abs(samco_pnl - state.daily_pnl):.2f} "
                    f"— FORCE SYNC from broker"
                )
                logger.warning("RECONCILE: %s", msg)
                result["issues_found"] += 1
                result["actions_taken"].append(msg)

                # AUTHORITATIVE ACTION: Update P&L from broker
                await self.state_manager.update(
                    daily_pnl=samco_pnl,
                    live_pnl=samco_pnl
                )

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
        while True:
            try:
                await asyncio.sleep(interval_seconds)

                now = datetime.now(IST)
                if now.weekday() < 5 and 9 <= now.hour < 16:
                    await self.run_once()

            except Exception as exc:
                logger.error("Reconciliation loop error: %s", exc)

    # ── Private helpers ──────────────────────────────────────

    async def _emergency_exit(self, symbol: str, qty: int) -> None:
        if qty <= 0:
            return

        for attempt in range(3):
            try:
                resp = await self.broker.place_order(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty
                )

                logger.critical("EMERGENCY EXIT: %s qty=%d", symbol, qty)

                await self.state_manager.update(
                    active_trade=None,
                    live_pnl=0.0
                )

                if self.event_bus:
                    await self.event_bus.publish("RECONCILE_EMERGENCY_EXIT", {
                        "symbol": symbol,
                        "qty": qty
                    })

                return

            except Exception as exc:
                if attempt == 2:
                    logger.critical("Emergency exit FAILED: %s", exc)
                await asyncio.sleep(1)

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

    async def _force_sync_from_broker(self, positions: list[dict]) -> None:
        """
        AUTHORITATIVE: Force synchronize internal state from broker positions.

        This method makes the broker the source of truth and overrides
        any conflicting internal state.
        """
        try:
            logger.info("🔧 FORCE SYNC: Updating internal state from broker positions")

            # Rebuild positions dict from broker data
            broker_positions = {}
            total_pnl = 0.0

            for pos in positions:
                symbol = pos.get("tradingSymbol", "")
                if not symbol or "NIFTY" not in symbol.upper():
                    continue

                net_qty = self._net_qty(pos)
                if net_qty == 0:
                    continue

                broker_positions[symbol] = net_qty

                # Extract P&L
                pnl = 0.0
                for key in ("pnl", "unrealizedPnl", "profitLoss"):
                    val = pos.get(key)
                    if val is not None:
                        try:
                            pnl = float(str(val).replace(",", "").replace("₹", "").strip())
                            break
                        except (ValueError, TypeError):
                            pass
                total_pnl += pnl

            # Update state with broker data
            await self.state_manager.update(
                positions=broker_positions,
                live_pnl=total_pnl,
                unrealized_pnl=total_pnl
            )

            # Reconstruct active trade if positions exist
            if broker_positions:
                # Find the primary position (largest quantity)
                primary_symbol = max(broker_positions.keys(),
                                   key=lambda s: abs(broker_positions[s]))

                # Find position details
                primary_pos = None
                for pos in positions:
                    if pos.get("tradingSymbol") == primary_symbol:
                        primary_pos = pos
                        break

                if primary_pos:
                    active_trade = {
                        "symbol": primary_symbol,
                        "qty": abs(broker_positions[primary_symbol]),
                        "entry_price": float(primary_pos.get("averagePrice") or
                                           primary_pos.get("avgPrice") or 0),
                        "current_price": float(primary_pos.get("ltp") or 0),
                        "entry_time": datetime.now(IST).isoformat(),  # Approximate
                        "unrealized_pnl": float(primary_pos.get("pnl") or 0)
                    }
                    entry_price = float(active_trade.get("entry_price") or 0.0)
                    if entry_price > 0:
                        active_trade["sl_price"] = round(
                            entry_price * (1 - settings.stop_loss_pct / 100), 2
                        )
                        active_trade["t1_price"] = round(
                            entry_price * (1 + settings.t1_pct / 100), 2
                        )
                        active_trade["t2_price"] = round(
                            entry_price * (1 + settings.t2_pct / 100), 2
                        )
                    await self.state_manager.update(active_trade=active_trade)
                    logger.info(f"✅ FORCE SYNC: Reconstructed active trade: {active_trade}")
                else:
                    logger.warning("FORCE SYNC: Could not find position details for reconstruction")
            else:
                # No positions, clear active trade
                await self.state_manager.update(active_trade=None)
                logger.info("✅ FORCE SYNC: Cleared active trade (no positions)")

            logger.info(f"✅ FORCE SYNC: Completed - {len(broker_positions)} positions, P&L: ₹{total_pnl}")

        except Exception as exc:
            logger.error(f"❌ FORCE SYNC failed: {exc}", exc_info=True)
            # Don't raise - reconciliation should continue
