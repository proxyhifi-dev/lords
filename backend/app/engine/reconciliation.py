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
import re
from datetime import datetime
from typing import Any
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
        self._last_paper_log = 0.0
        self.last_result: dict[str, Any] | None = None

    async def _update_truth_state(
        self,
        *,
        broker_position_count: int,
        reconstructed_ic_status: str,
        hedge_integrity_status: str,
        manual_intervention_required: bool,
    ) -> None:
        await self.state_manager.update(
            broker_position_count=int(broker_position_count),
            reconstructed_ic_status=reconstructed_ic_status,
            hedge_integrity_status=hedge_integrity_status,
            manual_intervention_required=manual_intervention_required,
        )

    async def _flag_pnl_mismatch(
        self,
        *,
        bot_daily_pnl: float,
        broker_daily_pnl: float,
        diff: float,
        has_active_trade: bool,
    ) -> None:
        reason = "reconciliation_pnl_mismatch_open_trade" if has_active_trade else "reconciliation_pnl_mismatch"
        updates = {
            "trading_enabled": False,
            "circuit_breaker_open": True,
            "last_order_failed": True,
            "last_risk_breach": reason,
        }
        await self.state_manager.update(**updates)

        if self.event_bus:
            await self.event_bus.publish(
                "RECONCILIATION_PNL_MISMATCH",
                {
                    "bot_daily_pnl": float(bot_daily_pnl),
                    "broker_daily_pnl": float(broker_daily_pnl),
                    "diff": float(diff),
                    "has_active_trade": bool(has_active_trade),
                },
            )

    # ── Public API ───────────────────────────────────────────

    async def run_once(self) -> dict:
        """
        Single reconciliation pass.
        Returns a summary dict with any issues found + actions taken.
        Call at startup and after any reconnect.
        """
        result: dict = {
            "timestamp":     datetime.now(IST).isoformat(),
            "issues_found":  0,
            "actions_taken": [],
            "status":        "ok",
        }

        # 🔥 FIX: Early exit for PAPER mode to save API calls & throttle logs
        if not settings.is_live:
            now = asyncio.get_event_loop().time()
            if now - self._last_paper_log > 300:
                logger.info("ℹ️ PAPER MODE: Broker reconciliation bypassed.")
                self._last_paper_log = now
            return result

        logger.info("Reconciliation check starting")

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
            await self._update_truth_state(
                broker_position_count=len(nifty_pos),
                reconstructed_ic_status="none" if not nifty_pos else "pending",
                hedge_integrity_status="unknown" if nifty_pos else "flat",
                manual_intervention_required=False,
            )

            # ── 1. Phantom position ───────────────────────
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
            if state.active_trade and not nifty_pos:
                confirmed_flat = await self._confirm_no_matching_open_position()
                if not confirmed_flat:
                    msg = (
                        "GHOST_TRADE verification uncertain — broker confirmation inconsistent. "
                        "Trading disabled for manual review."
                    )
                    logger.critical("RECONCILE: %s", msg)
                    result["issues_found"] += 1
                    result["actions_taken"].append(msg)
                    await self.state_manager.update(
                        trading_enabled=False,
                        circuit_breaker_open=True,
                        last_order_failed=True,
                        last_risk_breach="reconciliation_verification_uncertain",
                        manual_intervention_required=True,
                    )
                    await self._update_truth_state(
                        broker_position_count=0,
                        reconstructed_ic_status="verification_uncertain",
                        hedge_integrity_status="unknown",
                        manual_intervention_required=True,
                    )
                    self.last_result = dict(result)
                    return result

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
                    positions={},
                    reconstructed_ic_status="cleared_verified_flat",
                    hedge_integrity_status="flat",
                )

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
                            f"— FORCE SYNC from broker"
                        )
                        logger.warning("RECONCILE: %s", msg)
                        result["issues_found"] += 1
                        result["actions_taken"].append(msg)

                        # AUTHORITATIVE ACTION: Update quantities from broker
                        await self._force_sync_from_broker(positions)

            # ── 4. P&L drift ─────────────────────────────
            samco_pnl = self._sum_tradebook_pnl(trades)
            if samco_pnl != 0 and abs(samco_pnl - state.daily_pnl) > 500:
                msg = (
                    f"PNL_MISMATCH: bot=₹{state.daily_pnl:.2f} "
                    f"samco=₹{samco_pnl:.2f} "
                    f"diff=₹{abs(samco_pnl - state.daily_pnl):.2f} "
                    f"— FORCE SYNC from broker"
                )
                logger.warning("RECONCILE: %s", msg)
                result["issues_found"] += 1
                result["actions_taken"].append(msg)

                # Conservative action: fail closed for operator review.
                await self._flag_pnl_mismatch(
                    bot_daily_pnl=float(state.daily_pnl),
                    broker_daily_pnl=float(samco_pnl),
                    diff=abs(float(samco_pnl - state.daily_pnl)),
                    has_active_trade=bool(state.active_trade),
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

        self.last_result = dict(result)
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

    async def _confirm_no_matching_open_position(self) -> bool:
        positions = await self.broker.get_positions()
        nifty_pos = [
            p for p in positions
            if "NIFTY" in str(p.get("tradingSymbol", "")).upper()
            and self._net_qty(p) != 0
        ]
        return len(nifty_pos) == 0

    @staticmethod
    def _normalize_option_symbol(symbol: str) -> str:
        return re.sub(r"\s+", "", str(symbol or "").upper()).replace("NIFTY50", "NIFTY")

    @staticmethod
    def _parse_symbol_expiry(symbol: str) -> str | None:
        raw = str(symbol or "").strip()
        iso_match = re.search(r"(20\d{2}-\d{2}-\d{2})", raw)
        if iso_match:
            return iso_match.group(1)

        compact = ReconciliationEngine._normalize_option_symbol(raw)
        compact_match = re.search(r"(\d{1,2}[A-Z]{3}\d{2})", compact)
        if not compact_match:
            return None

        try:
            parsed = datetime.strptime(compact_match.group(1), "%d%b%y").date()
        except ValueError:
            return None
        return parsed.isoformat()

    def _extract_option_metadata(self, position: dict[str, Any]) -> dict[str, Any] | None:
        symbol = str(position.get("tradingSymbol") or position.get("symbolName") or "").strip()
        if not symbol:
            return None

        normalized_symbol = self._normalize_option_symbol(symbol)
        option_type = None
        if normalized_symbol.endswith("CE") or re.search(r"\bCE\b", symbol, re.IGNORECASE):
            option_type = "CE"
        elif normalized_symbol.endswith("PE") or re.search(r"\bPE\b", symbol, re.IGNORECASE):
            option_type = "PE"
        if option_type is None:
            return None

        strike = position.get("strikePrice") or position.get("strike")
        if strike is None:
            compact_match = re.search(
                r"\d{1,2}[A-Z]{3}\d{2}(\d{4,6})(?:CE|PE)$",
                normalized_symbol,
                re.IGNORECASE,
            )
            if compact_match:
                strike = compact_match.group(1)
            else:
                iso_match = re.search(
                    r"20\d{2}-\d{2}-\d{2}\s*(\d{4,6})(?=\s*(?:CE|PE)\b)",
                    symbol,
                    re.IGNORECASE,
                )
                if iso_match:
                    strike = iso_match.group(1)
        if strike is None:
            match = re.search(r"(\d{4,6})(?=(?:CE|PE)$)", normalized_symbol, re.IGNORECASE)
            if not match:
                match = re.search(r"(\d{4,6})(?=\s*(?:CE|PE)\b)", symbol, re.IGNORECASE)
            strike = match.group(1) if match else None
        try:
            strike_val = int(float(str(strike)))
        except (TypeError, ValueError):
            return None

        expiry = (
            position.get("expiryDate")
            or position.get("expiry")
            or position.get("expiry_date")
            or None
        )
        if expiry is None:
            expiry = self._parse_symbol_expiry(symbol)
        elif isinstance(expiry, str):
            stripped = expiry.strip()
            if stripped:
                try:
                    expiry = datetime.strptime(stripped, "%d %b %y").date().isoformat()
                except ValueError:
                    try:
                        expiry = datetime.strptime(stripped, "%d%b%Y").date().isoformat()
                    except ValueError:
                        try:
                            expiry = datetime.strptime(stripped, "%d%b%y").date().isoformat()
                        except ValueError:
                            expiry = stripped

        net_qty = self._net_qty(position)
        side = "BUY" if net_qty > 0 else "SELL"
        avg_price = 0.0
        for key in ("averagePrice", "avgPrice", "average_price"):
            val = position.get(key)
            if val is not None:
                try:
                    avg_price = float(str(val).replace(",", "").strip())
                    break
                except (TypeError, ValueError):
                    pass

        return {
            "symbol": symbol,
            "side": side,
            "qty": abs(net_qty),
            "net_qty": net_qty,
            "strike": strike_val,
            "option_type": option_type,
            "expiry": str(expiry) if expiry is not None else "",
            "entry_price": avg_price,
            "current_price": float(position.get("ltp") or 0.0),
            "display_symbol": symbol,
        }

    def _reconstruct_iron_condor_from_positions(self, positions: list[dict[str, Any]]) -> dict[str, Any]:
        legs = []
        for pos in positions:
            leg = self._extract_option_metadata(pos)
            if leg:
                legs.append(leg)

        if not legs:
            return {
                "status": "no_option_legs",
                "hedge_integrity_status": "flat",
                "manual_intervention_required": False,
                "trade": None,
            }

        ce_legs = [leg for leg in legs if leg["option_type"] == "CE"]
        pe_legs = [leg for leg in legs if leg["option_type"] == "PE"]
        if len(legs) != 4 or len(ce_legs) != 2 or len(pe_legs) != 2:
            return {
                "status": "partial_or_orphan_legs",
                "hedge_integrity_status": "broken",
                "manual_intervention_required": True,
                "trade": None,
            }

        qtys = {int(leg["qty"]) for leg in legs if int(leg["qty"]) > 0}
        expiries = {str(leg["expiry"]) for leg in legs if str(leg["expiry"]).strip()}
        if len(qtys) != 1 or len(expiries) > 1:
            return {
                "status": "mismatched_qty_or_expiry",
                "hedge_integrity_status": "broken",
                "manual_intervention_required": True,
                "trade": None,
            }

        short_calls = [leg for leg in ce_legs if leg["side"] == "SELL"]
        long_calls = [leg for leg in ce_legs if leg["side"] == "BUY"]
        short_puts = [leg for leg in pe_legs if leg["side"] == "SELL"]
        long_puts = [leg for leg in pe_legs if leg["side"] == "BUY"]
        if not (len(short_calls) == len(long_calls) == len(short_puts) == len(long_puts) == 1):
            return {
                "status": "broken_hedge",
                "hedge_integrity_status": "broken",
                "manual_intervention_required": True,
                "trade": None,
            }

        short_call = short_calls[0]
        long_call = long_calls[0]
        short_put = short_puts[0]
        long_put = long_puts[0]
        hedge_ok = long_call["strike"] > short_call["strike"] and long_put["strike"] < short_put["strike"]
        if not hedge_ok:
            return {
                "status": "broken_hedge",
                "hedge_integrity_status": "broken",
                "manual_intervention_required": True,
                "trade": None,
            }

        qty = qtys.pop()
        expiry = next(iter(expiries), "")
        entry_premium = round(
            float(short_call["entry_price"])
            - float(long_call["entry_price"])
            + float(short_put["entry_price"])
            - float(long_put["entry_price"]),
            2,
        )
        trade = {
            "strategy": "IRON_CONDOR",
            "signal": "IRON_CONDOR",
            "symbol": "NIFTY",
            "underlying": "NIFTY 50",
            "status": "OPEN",
            "entry_time": datetime.now(IST).isoformat(),
            "expiry": expiry,
            "qty": qty,
            "entry_price": entry_premium if entry_premium > 0 else 0.0,
            "strike": f"{short_call['strike']}/{short_put['strike']}",
            "strikes": {
                "short_call": short_call["strike"],
                "long_call": long_call["strike"],
                "short_put": short_put["strike"],
                "long_put": long_put["strike"],
                "call_width": abs(long_call["strike"] - short_call["strike"]),
                "put_width": abs(short_put["strike"] - long_put["strike"]),
            },
            "legs": [
                {**short_call, "name": "short_call"},
                {**long_call, "name": "long_call"},
                {**short_put, "name": "short_put"},
                {**long_put, "name": "long_put"},
            ],
            "current_legs": [
                {**short_call, "name": "short_call"},
                {**long_call, "name": "long_call"},
                {**short_put, "name": "short_put"},
                {**long_put, "name": "long_put"},
            ],
            "pricing_source": "broker_position_reconstruction",
            "current_pricing_source": "broker_position_reconstruction",
            "manual_intervention_required": True,
            "reconstructed_from_broker": True,
        }
        return {
            "status": "reconstructed_ic",
            "hedge_integrity_status": "intact",
            "manual_intervention_required": True,
            "trade": trade,
        }

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
                reconstruction = self._reconstruct_iron_condor_from_positions(positions)
                status = str(reconstruction["status"])
                hedge_status = str(reconstruction["hedge_integrity_status"])
                manual_review = bool(reconstruction["manual_intervention_required"])
                active_trade = reconstruction.get("trade")

                if active_trade:
                    await self.state_manager.update(
                        active_trade=active_trade,
                        trading_enabled=False,
                        circuit_breaker_open=True,
                        last_order_failed=True,
                        last_risk_breach="reconciliation_broker_reconstruction",
                        manual_intervention_required=manual_review,
                        reconstructed_ic_status=status,
                        hedge_integrity_status=hedge_status,
                    )
                    logger.info("✅ FORCE SYNC: Reconstructed IC trade from broker status=%s", status)
                else:
                    await self.state_manager.update(
                        active_trade=None,
                        trading_enabled=False,
                        circuit_breaker_open=True,
                        last_order_failed=True,
                        last_risk_breach="reconciliation_broker_state_uncertain",
                        manual_intervention_required=True,
                        reconstructed_ic_status=status,
                        hedge_integrity_status=hedge_status,
                    )
                    logger.warning("FORCE SYNC: broker positions present but reconstruction confidence is low status=%s", status)
            else:
                # No positions, clear active trade
                await self.state_manager.update(
                    active_trade=None,
                    reconstructed_ic_status="flat",
                    hedge_integrity_status="flat",
                    manual_intervention_required=False,
                )
                logger.info("✅ FORCE SYNC: Cleared active trade (no positions)")

            logger.info(f"✅ FORCE SYNC: Completed - {len(broker_positions)} positions, P&L: ₹{total_pnl}")

        except Exception as exc:
            logger.error(f"❌ FORCE SYNC failed: {exc}", exc_info=True)
            # Don't raise - reconciliation should continue
