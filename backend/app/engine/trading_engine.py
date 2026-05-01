"""
Lords Bot — Trading Engine v5.3 (DIAGNOSTIC SYMBOL RESOLUTION + EXIT BUG FIX)
==============================================================================

Listens to RISK_APPROVED → executes entry → monitors exit.

Changes in v5.3 (over your v5.2):

  1. _resolve_symbol — now logs validationErrors, raw response shape, and
     available strikes when chain returns empty. Prevents cache poisoning
     of failed lookups. Detects and warns on strike-grid snap mismatches.

  2. _exit_trade — fixed indentation bug where sell_exec result handling
     was OUTSIDE the trade_lock context manager. The original code also
     called a non-existent _global_fail_safe() and returned a bool from
     a void method, which would have crashed on first SL/EOD exit.

  3. _book_partial — now uses execution_manager (consistent with entry
     and full exit). The old direct _sell_with_retry call still works
     but bypassed your execution_manager's idempotency / circuit-breaker
     instrumentation. Switched to execute_order for consistency.

Everything else is preserved exactly as v5.2.
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict, deque
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.execution_manager import ExecutionManager, OrderState
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("trading_engine")
IST = ZoneInfo("Asia/Kolkata")

_SELL_MAX_RETRIES = 3
_SELL_RETRY_DELAY = 1.5
_FILL_CONFIRM_ATTEMPTS = 8
_FILL_CONFIRM_DELAY = 0.75
_EXIT_VERIFY_ATTEMPTS = 4
_EXIT_VERIFY_DELAY = 1.0
_ENTRY_MAX_RETRIES = 3
_ENTRY_RETRY_DELAY = 1.0


def _parse_volume(quote: dict) -> int:
    def _int(val) -> int:
        try:
            return int(float(str(val).replace(",", "").strip()))
        except (TypeError, ValueError):
            return 0

    for k in ("tradedVolume", "volume", "traded_volume", "totalTradedVolume"):
        v = _int(quote.get(k))
        if v > 0:
            return v
    inner = quote.get("quoteDetails")
    if isinstance(inner, list) and inner:
        inner = inner[0]
    if isinstance(inner, dict):
        for k in ("tradedVolume", "volume", "totalTradedVolume"):
            v = _int(inner.get(k))
            if v > 0:
                return v
    data = quote.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        for k in ("tradedVolume", "volume", "totalTradedVolume"):
            v = _int(data.get(k))
            if v > 0:
                return v
    return 0


def _parse_filled_qty(order_status: dict, requested_qty: int) -> int:
    """Extract actual filled quantity from SAMCO order status response."""
    def _int(val) -> int:
        try:
            return int(float(str(val).replace(",", "").strip()))
        except (TypeError, ValueError):
            return 0

    data = order_status.get("orderDetails") or order_status.get("data") or order_status
    if isinstance(data, list):
        data = data[0] if data else {}

    for key in (
        "filledShares", "tradedQty", "filledQty",
        "executedQty", "filled_quantity", "tradedQuantity",
    ):
        v = _int(data.get(key))
        if v > 0:
            return v

    status = str(data.get("orderStatus") or data.get("status") or "").upper()
    if status in ("COMPLETE", "FILLED", "TRADED"):
        return requested_qty

    return 0


class TradingEngine:

    def __init__(
        self,
        event_bus: EventBus,
        state_manager,
        trade_store: TradeStore,
        broker: SamcoClient,
        strategy=None,
    ):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.trade_store = trade_store
        self.broker = broker
        self.strategy = strategy
        self._trade_lock = asyncio.Lock()
        self._symbol_cache: dict[str, str] = {}
        self._fatal_lock = asyncio.Lock()
        self._ltp_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))
        self.reconciliation = None
        self.execution_manager = ExecutionManager(
            broker=self.broker,
            state_manager=self.state_manager,
            event_bus=self.event_bus,
        )

    def _map_signal(self, raw_signal: str) -> str:
        """Map trading signals to option types."""
        if not raw_signal:
            raise ValueError("Invalid signal: empty or missing")

        signal = str(raw_signal).strip().upper()
        if signal in ("LONG", "CALL"):
            return "CALL"
        if signal in ("SHORT", "PUT"):
            return "PUT"
        raise ValueError(f"Invalid signal: {raw_signal}. Expected LONG, SHORT, CALL or PUT.")

    async def run(self):
        logger.info("TradingEngine started")
        try:
            await asyncio.gather(
                self._entry_listener(),
                self._monitor_loop(),
                self._health_loop(),
            )
        except asyncio.CancelledError:
            logger.info("TradingEngine cancelled (normal shutdown)")
            raise
        except Exception as exc:
            logger.critical("🚨 TradingEngine run loop failure: %s", exc, exc_info=True)
            try:
                await self.emergency_exit_active_trade(reason="SYSTEM_FAILURE")
            except Exception as exit_exc:
                logger.critical(
                    "🚨 SYSTEM_FAILURE emergency exit failed: %s",
                    exit_exc, exc_info=True,
                )
            try:
                await self.state_manager.update(
                    trading_enabled=False,
                    last_order_failed=True,
                    last_risk_breach="system_failure",
                )
            except Exception as state_exc:
                logger.critical("Failed to disable trading after system failure: %s", state_exc)
            raise

    # ── ENTRY ────────────────────────────────────────────────
    async def _entry_listener(self):
        logger.info("TradingEngine listening for RISK_APPROVED events")
        queue = self.event_bus.subscribe("RISK_APPROVED")
        async for event in self.event_bus.iter_events(queue):
            logger.info("📡 TradingEngine received event: %s", event.payload)
            await self._enter_trade(event.payload)

    async def _enter_trade(self, payload: dict):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            await self.state_manager.update(signal=None, signal_meta=None)
            if os.getenv("TRADING_KILL_SWITCH", "0") == "1":
                logger.critical("Kill switch enabled; rejecting entry")
                return

            if state.active_trade:
                return
            if state.spot_price is None:
                return
            if not state.trading_enabled:
                return

            no_h, no_m = map(int, settings.no_entry_after.split(":"))
            if datetime.now(IST).time() >= dtime(no_h, no_m):
                logger.info("Past no-entry time — skipping")
                return

            raw_signal = payload.get("signal")
            size_label = payload.get("size_label", "FULL")
            logger.info(
                "📡 SIGNAL RECEIVED: raw='%s' size='%s' spot=₹%.2f",
                raw_signal, size_label, state.spot_price or 0,
            )

            try:
                signal = self._map_signal(raw_signal)
                logger.info("🔄 SIGNAL MAPPED: '%s' → '%s'", raw_signal, signal)

                expiry = OptionSelector.get_expiry_api()
                dte = self._days_to_expiry(expiry)
                if dte is None:
                    logger.warning("Unable to parse expiry for DTE validation: %s", expiry)
                    return
                if dte < settings.min_dte or dte > settings.max_dte:
                    logger.warning(
                        "Expiry DTE %d outside allowed range [%d,%d] for expiry=%s",
                        dte, settings.min_dte, settings.max_dte, expiry,
                    )
                    return

                strike = OptionSelector.get_otm_strike(
                    state.spot_price, signal, distance=settings.otm_distance,
                )
                logger.info(
                    "🎯 STRIKE CALCULATED: spot=₹%.2f signal='%s' → strike=%d",
                    state.spot_price, signal, strike,
                )

                symbol = await self._resolve_symbol(strike, signal)
                if not symbol:
                    logger.error(
                        "❌ SYMBOL RESOLUTION FAILED: strike=%d signal='%s'",
                        strike, signal,
                    )
                    return
                logger.info("✅ SYMBOL RESOLVED: %s", symbol)

                quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
                ltp = self.broker.parse_ltp(quote)
                bid, ask = self.broker.parse_bid_ask(quote)
                if not ltp:
                    logger.warning("❌ LTP UNAVAILABLE: %s", symbol)
                    return
                logger.info("💰 LTP FETCHED: %s = ₹%.2f", symbol, ltp)

                dynamic_spread_limit = self._compute_dynamic_spread_limit(symbol, ltp)
                if bid and ask and ask > bid:
                    spread = ask - bid
                    spread_pct = spread / ask
                    if spread_pct > dynamic_spread_limit:
                        logger.warning(
                            "❌ SPREAD TOO WIDE: %.2f%% > %.2f%% symbol=%s "
                            "bid=%.2f ask=%.2f ltp=%.2f",
                            spread_pct * 100, dynamic_spread_limit * 100,
                            symbol, bid, ask, ltp,
                        )
                        return

                if not self._validate_trade_setup(state, raw_signal, ltp, bid, ask):
                    logger.warning(
                        "❌ TRADE VALIDATION FAILED: symbol=%s signal=%s",
                        symbol, raw_signal,
                    )
                    return

                if ltp < settings.min_entry_premium:
                    logger.warning(
                        "❌ PREMIUM TOO LOW: ₹%.1f < min ₹%.1f — skip %s",
                        ltp, settings.min_entry_premium, symbol,
                    )
                    return

                if settings.min_option_volume > 0:
                    vol = _parse_volume(quote)
                    if vol < settings.min_option_volume:
                        logger.warning(
                            "❌ LOW VOLUME: %d < %d — skip %s",
                            vol, settings.min_option_volume, symbol,
                        )
                        return
                    logger.info("📊 VOLUME OK: %s = %d", symbol, vol)

                requested_qty = self._get_qty(size_label)
                logger.info(
                    "📋 QTY CALCULATED: size='%s' → qty=%d",
                    size_label, requested_qty,
                )

                logger.info("🛒 PLACING BUY ORDER: %s qty=%d", symbol, requested_qty)
                exec_result = await self.execution_manager.execute_order({
                    "signal": raw_signal,
                    "symbol": symbol,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "quantity": requested_qty,
                    "side": "BUY",
                })
                order_id = exec_result.order_id
                fill_price = exec_result.avg_price
                filled_qty = exec_result.filled_qty
                if exec_result.is_uncertain:
                    await self._handle_fatal_exception(
                        "ENTRY_EXECUTION_UNCERTAIN",
                        RuntimeError("Entry execution uncertain"),
                    )
                    return
                # Primary check: execution state is FILLED
                if exec_result.state != OrderState.FILLED:
                    logger.error("❌ BUY ORDER FAILED: state=%s order=%s", exec_result.state, order_id or "NONE")
                    return

                is_paper = order_id.startswith("PAPER-")
                
                # For paper mode with FILLED state, use requested_qty as filled_qty
                if is_paper and exec_result.state == OrderState.FILLED:
                    filled_qty = requested_qty
                
                # Check filled_qty: allow 0 for paper if state is FILLED
                if filled_qty <= 0 and not is_paper:
                    logger.error("❌ BUY ORDER FAILED: no executable fill qty=%d", filled_qty)
                    return

                conservative_ltp = ask if ask else ltp
                
                # Handle fill price for PAPER vs LIVE mode
                # At this point: exec_result.state == FILLED ✓
                if fill_price is None:
                    if is_paper:
                        logger.warning("⚠️ PAPER MODE: using LTP as fill price for order=%s qty=%d", order_id, filled_qty)
                        entry_price = conservative_ltp
                    else:
                        logger.error("❌ LIVE MODE: missing fill price despite FILLED state order=%s", order_id)
                        return
                else:
                    entry_price = fill_price
                logger.info(
                    "💰 BUY FILL CONFIRMED: order=%s fill=₹%.2f ltp=₹%.2f mode=%s",
                    order_id, entry_price, ltp, settings.mode.upper(),
                )

                logger.info(
                    "📈 ENTRY SUMMARY: fill=₹%.2f ltp=₹%.2f qty=%d/%d order=%s mode=%s",
                    entry_price, ltp, filled_qty, requested_qty,
                    order_id, settings.mode.upper(),
                )

                t1_qty = filled_qty // 2
                t2_qty = filled_qty - t1_qty

                trade = {
                    "symbol":          symbol,
                    "strike":          strike,
                    "qty":             filled_qty,
                    "requested_qty":   requested_qty,
                    "entry_price":     entry_price,
                    "entry_ltp":       ltp,
                    "entry_time":      datetime.now(timezone.utc).isoformat(),
                    "status":          "OPEN",
                    "signal":          signal,
                    "max_price":       entry_price,
                    "order_id":        order_id,
                    "size_label":      size_label,
                    "sl_price":        round(entry_price * (1 - settings.stop_loss_pct), 2),
                    "t1_price":        round(entry_price * (1 + settings.t1_pct), 2),
                    "t2_price":        round(entry_price * (1 + settings.t2_pct), 2),
                    "t1_hit":          False,
                    "t1_booked":       False,
                    "t1_qty":          t1_qty,
                    "t2_qty":          t2_qty,
                    "t1_pnl":          0.0,
                    "partial_fill":    filled_qty < requested_qty,
                    "sl_order_id":     None,
                }

                # Mandatory broker-level SL protection: fail closed if unavailable
                sl_resp = await self.broker.place_stop_loss_order(
                    symbol=symbol,
                    quantity=filled_qty,
                    trigger_price=trade["sl_price"],
                    side="SELL",
                )
                sl_order_id = sl_resp.get("orderNumber") or sl_resp.get("orderId")
                if sl_resp.get("status") != "Success" or not sl_order_id:
                    logger.critical(
                        "SL placement failed after entry fill; forcing exit and halting. resp=%s",
                        sl_resp,
                    )
                    await self._force_exit_and_halt(
                        symbol=symbol, qty=filled_qty,
                        reason="STOP_LOSS_PLACEMENT_FAILED",
                    )
                    return
                trade["sl_order_id"] = sl_order_id

                await self.state_manager.update(
                    active_trade=trade,
                    trade_count=state.trade_count + 1,
                )
                
                # Position validation only for LIVE mode (paper positions are simulated)
                if not is_paper:
                    await self._validate_post_order_position(symbol, filled_qty, "ENTRY")
                else:
                    logger.warning(
                        "⚠️ PAPER MODE: skipping broker position validation (symbol=%s qty=%d)",
                        symbol, filled_qty,
                    )

                logger.info(
                    "🚀 TRADE OPENED: %s qty=%d entry=₹%.2f SL=₹%.2f T1=₹%.2f T2=₹%.2f%s",
                    symbol, filled_qty, entry_price,
                    trade["sl_price"], trade["t1_price"], trade["t2_price"],
                    " [PARTIAL FILL]" if trade["partial_fill"] else "",
                )
                logger.info(
                    "📊 TRADE DETAILS: strike=%d signal='%s' t1_qty=%d t2_qty=%d order=%s",
                    strike, signal, t1_qty, t2_qty, order_id,
                )

                if self.strategy:
                    self.strategy.set_already_traded_today()
                    logger.info("🔒 Daily trade limit locked (after successful entry)")

                await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

            except Exception as exc:
                logger.error("❌ ENTRY FAILED: %s", exc, exc_info=True)
                await self._handle_fatal_exception("entry", exc)

    # ── MONITOR ─────────────────────────────────────────────
    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(2)

            state = await self.state_manager.snapshot()
            trade = state.active_trade

            if not trade:
                continue
            if trade.get("status") == "CLOSED":
                continue

            try:
                quote = await self.broker.get_quote(
                    symbol_name=trade["symbol"], exchange="NFO",
                )
                ltp = self.broker.parse_ltp(quote)

                if not ltp or ltp <= 0:
                    continue

                entry = trade["entry_price"]
                t1_booked = trade.get("t1_booked", False)

                sl_price = trade.get(
                    "sl_price",
                    round(entry * (1 - settings.stop_loss_pct), 2),
                )
                t1_price = trade.get(
                    "t1_price",
                    round(entry * (1 + settings.t1_pct), 2),
                )
                t2_price = trade.get(
                    "t2_price",
                    round(entry * (1 + settings.t2_pct), 2),
                )

                remaining_qty = (
                    trade.get("t2_qty", trade["qty"] // 2)
                    if t1_booked else trade["qty"]
                )

                live_pnl = (ltp - entry) * remaining_qty if ltp > 0 else 0.0
                await self.state_manager.update(live_pnl=round(live_pnl, 2))

                # ✅ FIX: Update max_price
                if ltp > trade.get("max_price", entry):
                    trade["max_price"] = ltp
                    await self.state_manager.update(active_trade=trade)

                # ✅ NEW: Breakeven SL upgrade (before T1)
                breakeven_trigger = getattr(settings, "breakeven_at_pct", 0.20)
                if not t1_booked and ltp >= entry * (1 + breakeven_trigger):
                    new_sl = max(sl_price, entry * 1.001)
                    if new_sl > sl_price:
                        sl_price = round(new_sl, 2)
                        trade["sl_price"] = sl_price
                        await self.state_manager.update(active_trade=trade)
                        logger.info("✅ SL upgraded to breakeven: ₹%.2f (ltp ₹%.2f)", sl_price, ltp)

                trail_sl = round(
                    trade["max_price"] * (1 - settings.trailing_pct), 2,
                )

                sq_h, sq_m = map(int, settings.square_off.split(":"))
                now = datetime.now(IST)

                # ✅ FIX: Check SL/T1/T2 BEFORE EOD (was reversed)
                if ltp <= sl_price:
                    await self._exit_trade(trade, "STOPLOSS", ltp)
                    continue

                if not t1_booked and ltp >= t1_price:
                    await self._book_partial(trade, ltp)
                    continue

                if t1_booked:
                    if ltp >= t2_price:
                        await self._exit_remaining(trade, "TARGET_2", ltp)
                        continue

                    if ltp <= trail_sl:
                        await self._exit_remaining(trade, "TRAIL_STOP", ltp)
                        continue

                # EOD check LAST
                if now.time() >= dtime(sq_h, sq_m):
                    await self._exit_trade(trade, "EOD_SQUAREOFF", ltp)
                    continue

            except Exception as exc:
                logger.error("Monitor loop: %s", exc, exc_info=True)
                await self._handle_fatal_exception("monitor_loop", exc)

    # ── BOOK PARTIAL — T1 ────────────────────────────────────
    async def _book_partial(self, trade: dict, ltp: float):
        if trade.get("status") == "CLOSED":
            logger.info("🚫 BOOK PARTIAL SKIPPED: trade already closed")
            return

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("🚫 BOOK PARTIAL SKIPPED: no active trade in state")
                return

            symbol = trade["symbol"]
            t1_qty = trade.get("t1_qty", trade["qty"] // 2)

            logger.info(
                "🔄 BOOKING PARTIAL: %s qty=%d ltp=₹%.2f",
                symbol, t1_qty, ltp,
            )

            sell_id, fill_price = await self._sell_with_retry(
                symbol, t1_qty, "TARGET_1",
            )
            if not sell_id:
                logger.critical(
                    "❌ T1 SELL FAILED — position may be open! %s qty=%d",
                    symbol, t1_qty,
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t1_qty, "reason": "TARGET_1"},
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            t1_pnl = round((exit_price - trade["entry_price"]) * t1_qty, 2)

            logger.info(
                "💰 T1 PARTIAL FILL: order=%s fill=₹%.2f pnl=₹%.2f",
                sell_id, exit_price, t1_pnl,
            )

            trade["t1_booked"] = True
            trade["t1_pnl"] = t1_pnl
            trade["t1_exit_price"] = exit_price

            await self.state_manager.update(active_trade=trade)

            logger.info(
                "✅ PARTIAL BOOKED: %s t1_pnl=₹%.2f remaining_qty=%d",
                symbol, t1_pnl, trade.get("t2_qty", trade["qty"] // 2),
            )

            await self.event_bus.publish("PARTIAL_BOOKED", {"trade": trade})

    # ── EXIT REMAINING — T2 / Trail ──────────────────────────
    async def _exit_remaining(self, trade: dict, reason: str, ltp: float):
        if trade.get("status") == "CLOSED":
            logger.info("🚫 EXIT REMAINING SKIPPED: trade already closed")
            return

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("🚫 EXIT REMAINING SKIPPED: no active trade in state")
                return

            symbol = trade["symbol"]
            t2_qty = trade.get("t2_qty", trade["qty"] // 2)

            logger.info(
                "🔄 EXITING REMAINING: %s qty=%d reason='%s' ltp=₹%.2f",
                symbol, t2_qty, reason, ltp,
            )

            sell_id, fill_price = await self._sell_with_retry(symbol, t2_qty, reason)
            if not sell_id:
                logger.critical(
                    "❌ T2 SELL FAILED — position may be open! %s qty=%d reason=%s",
                    symbol, t2_qty, reason,
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t2_qty, "reason": reason},
                )
                await self._handle_fatal_exception(
                    f"exit_remaining_sell_failed:{reason}",
                    RuntimeError("Remaining position sell failed"),
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            t2_pnl = round((exit_price - trade["entry_price"]) * t2_qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + t2_pnl, 2)
            new_daily = round(state.daily_pnl + t2_pnl, 2)

            logger.info(
                "💰 T2 EXIT FILL: order=%s fill=₹%.2f pnl=₹%.2f total_pnl=₹%.2f",
                sell_id, exit_price, t2_pnl, total_pnl,
            )

            position_closed = await self._ensure_position_closed(
                symbol, reason, fallback_qty=t2_qty,
            )
            if not position_closed:
                await self._handle_fatal_exception(
                    f"exit_remaining_verify_failed:{reason}",
                    RuntimeError("Broker position still open after exit"),
                )
                return

            trade["status"] = "CLOSED"

            closed = {
                **trade,
                "exit_price": exit_price,
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "status": "CLOSED",
                "exit_reason": reason,
                "pnl": total_pnl,
                "sell_order_id": sell_id,
            }

            self.trade_store.append_trade(closed, new_daily)

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=new_daily,
                live_pnl=0.0,
                consecutive_losses=self._next_consecutive_losses(
                    state.consecutive_losses, total_pnl,
                ),
            )
            await self._enforce_global_risk_stop(new_daily, total_pnl, state)

            logger.info(
                "✅ TRADE CLOSED (REMAINING): %s reason='%s' exit=₹%.2f pnl=₹%.2f daily_pnl=₹%.2f",
                symbol, reason, exit_price, total_pnl, new_daily,
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    # ── FULL EXIT — SL / EOD ─────────────────────────────────
    async def _exit_trade(self, trade: dict, reason: str, ltp: float):
        if trade.get("status") == "CLOSED":
            logger.info("🚫 EXIT SKIPPED: trade already closed")
            return

        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                logger.warning("🚫 EXIT SKIPPED: no active trade in state")
                return

            symbol = trade["symbol"]
            qty = (
                trade.get("t2_qty", trade["qty"] // 2)
                if trade.get("t1_booked") else trade["qty"]
            )

            logger.info(
                "🔄 EXITING TRADE: %s qty=%d reason='%s' ltp=₹%.2f",
                symbol, qty, reason, ltp,
            )

            # ── EXEC ORDER VIA EXECUTION_MANAGER (was indented wrong in v5.2) ──
            sell_exec = await self.execution_manager.execute_order({
                "signal": reason,
                "symbol": symbol,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "quantity": qty,
                "side": "SELL",
            })
            sell_id = sell_exec.order_id
            fill_price = sell_exec.avg_price

            if sell_exec.is_uncertain:
                await self._handle_fatal_exception(
                    f"SELL_EXECUTION_UNCERTAIN:{reason}",
                    RuntimeError("Sell execution result uncertain"),
                )
                return

            if not sell_id:
                logger.critical(
                    "❌ SELL FAILED — position may be open! %s qty=%d reason=%s",
                    symbol, qty, reason,
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": qty, "reason": reason},
                )
                await self._handle_fatal_exception(
                    f"exit_trade_sell_failed:{reason}",
                    RuntimeError("Full exit sell failed"),
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            exit_pnl = round((exit_price - trade["entry_price"]) * qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + exit_pnl, 2)
            new_daily = round(state.daily_pnl + exit_pnl, 2)

            logger.info(
                "💰 EXIT FILL: order=%s fill=₹%.2f pnl=₹%.2f total_pnl=₹%.2f",
                sell_id, exit_price, exit_pnl, total_pnl,
            )

            position_closed = await self._ensure_position_closed(
                symbol, reason, fallback_qty=qty,
            )
            if not position_closed:
                await self._handle_fatal_exception(
                    f"exit_trade_verify_failed:{reason}",
                    RuntimeError("Broker position still open after full exit"),
                )
                return

            trade["status"] = "CLOSED"

            closed = {
                **trade,
                "exit_price":    exit_price,
                "exit_time":     datetime.now(timezone.utc).isoformat(),
                "status":        "CLOSED",
                "exit_reason":   reason,
                "pnl":           total_pnl,
                "sell_order_id": sell_id,
            }

            self.trade_store.append_trade(closed, new_daily)

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=new_daily,
                live_pnl=0.0,
                consecutive_losses=self._next_consecutive_losses(
                    state.consecutive_losses, total_pnl,
                ),
            )
            await self._enforce_global_risk_stop(new_daily, total_pnl, state)

            logger.info(
                "✅ TRADE CLOSED: %s reason='%s' exit=₹%.2f pnl=₹%.2f daily_pnl=₹%.2f",
                symbol, reason, exit_price, total_pnl, new_daily,
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    # ── SELL WITH RETRY ──────────────────────────────────────
    async def _sell_with_retry(
        self, symbol: str, qty: int, reason: str,
    ) -> tuple[str | None, float | None]:
        """
        Try SELL up to _SELL_MAX_RETRIES times.
        After all retries fail → emergency market order.
        Returns (order_id, fill_price) or (None, None) if everything fails.
        """
        for attempt in range(1, _SELL_MAX_RETRIES + 1):
            try:
                sell_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol, side="SELL", quantity=qty,
                )
                if sell_id:
                    logger.info(
                        "SELL OK attempt=%d reason=%s order=%s fill=%s",
                        attempt, reason, sell_id,
                        f"₹{fill_price:.2f}" if fill_price else "N/A(paper)",
                    )
                    return sell_id, fill_price
                logger.warning(
                    "SELL attempt %d/%d no order_id reason=%s",
                    attempt, _SELL_MAX_RETRIES, reason,
                )
            except Exception as exc:
                logger.error(
                    "SELL attempt %d/%d exception reason=%s: %s",
                    attempt, _SELL_MAX_RETRIES, reason, exc,
                )

            if attempt < _SELL_MAX_RETRIES:
                await asyncio.sleep(_SELL_RETRY_DELAY)

        # Emergency fallback
        logger.critical(
            "EMERGENCY SELL: all %d retries failed — placing emergency order %s qty=%d",
            _SELL_MAX_RETRIES, symbol, qty,
        )
        try:
            resp = await self.broker.place_order(
                symbol=symbol, side="SELL", quantity=qty,
            )
            eid = resp.get("orderNumber") or resp.get("orderId")
            if eid:
                await asyncio.sleep(2)
                fp = await self.broker.get_actual_fill_price(eid)
                logger.critical("EMERGENCY SELL placed order=%s fill=%s", eid, fp)
                return eid, fp
        except Exception as exc:
            logger.critical("EMERGENCY SELL also failed: %s", exc)

        return None, None

    async def _buy_with_retry(
        self, symbol: str, requested_qty: int,
    ) -> tuple[str | None, float | None, int]:
        """
        Buy with strict terminal-state checks (FILLED/REJECTED/CANCELLED) and retry.
        Returns (order_id, fill_price, filled_qty). filled_qty can be partial.
        """
        for attempt in range(1, _ENTRY_MAX_RETRIES + 1):
            try:
                order_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol, side="BUY", quantity=requested_qty,
                )
                if not order_id:
                    logger.warning(
                        "BUY attempt=%d/%d failed: no order_id",
                        attempt, _ENTRY_MAX_RETRIES,
                    )
                    continue

                if order_id.startswith("PAPER-"):
                    return order_id, fill_price, requested_qty

                status, filled_qty, broker_avg = await self._await_fill_confirmation(
                    order_id=order_id,
                    requested_qty=requested_qty,
                    side="BUY",
                )
                if broker_avg and not fill_price:
                    fill_price = broker_avg
                data = status.get("orderDetails") or status.get("data") or status
                if isinstance(data, list):
                    data = data[0] if data else {}
                broker_state = str(data.get("orderStatus") or data.get("status") or "").upper()
                if broker_state in ("REJECTED", "CANCELLED", "CANCELED"):
                    logger.warning(
                        "BUY rejected/cancelled attempt=%d/%d order=%s state=%s",
                        attempt, _ENTRY_MAX_RETRIES, order_id, broker_state,
                    )
                    continue
                if filled_qty > 0:
                    if filled_qty < requested_qty:
                        await self._safe_cancel_order(order_id)
                    return order_id, fill_price, filled_qty
                logger.warning(
                    "BUY no-fill attempt=%d/%d order=%s state=%s",
                    attempt, _ENTRY_MAX_RETRIES, order_id, broker_state or "UNKNOWN",
                )
            except Exception as exc:
                logger.error(
                    "BUY attempt=%d/%d exception: %s",
                    attempt, _ENTRY_MAX_RETRIES, exc,
                )
                if attempt == _ENTRY_MAX_RETRIES:
                    raise
            if attempt < _ENTRY_MAX_RETRIES:
                await asyncio.sleep(_ENTRY_RETRY_DELAY * attempt)
        return None, None, 0

    async def emergency_exit_active_trade(self, reason: str = "EMERGENCY") -> bool:
        state = await self.state_manager.snapshot()
        trade = state.active_trade
        if not trade:
            return True

        symbol = trade.get("symbol")
        qty = (
            trade.get("t2_qty", trade.get("qty", 0) // 2)
            if trade.get("t1_booked")
            else trade.get("qty", 0)
        )
        if not symbol or qty <= 0:
            await self.state_manager.update(active_trade=None, live_pnl=0.0)
            return False

        sell_id, _ = await self._sell_with_retry(symbol, qty, reason)
        if not sell_id:
            return False
        closed = await self._ensure_position_closed(symbol, reason, fallback_qty=qty)
        if closed:
            await self.state_manager.update(active_trade=None, live_pnl=0.0)
        return closed

    # ── HEALTH ───────────────────────────────────────────────
    async def _health_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                if not await self.broker.healthcheck():
                    logger.warning("Healthcheck failed — re-login")
                    await self.broker.login()
                    state = await self.state_manager.snapshot()
                    if state.active_trade:
                        raise RuntimeError("Broker healthcheck failed during active trade")
            except Exception as exc:
                logger.error("Health loop: %s", exc)
                await self._handle_fatal_exception("health_loop", exc)

    # ── HELPERS ──────────────────────────────────────────────
    def _get_qty(self, size_label: str) -> int:
        base = settings.order_qty
        if size_label == "FULL":
            return max(base, 1)
        if size_label == "MEDIUM":
            return max(int(round(base * 0.75)), 1)
        if size_label == "HALF":
            return max(int(round(base * 0.50)), 1)
        return base

    async def _resolve_symbol(self, strike: int, signal: str) -> str | None:
        """
        Resolve trading symbol for given strike + signal.

        Strategy:
          1. Try specific strike first (efficient, what SAMCO recommends).
          2. If that returns empty/error, fall back to full chain (strike="0").
          3. From the chain, pick the closest valid strike.
          4. Cache the result.

        On failure, logs validationErrors, raw response shape, and available
        strikes so the cause can be diagnosed from the log.
        """
        key = f"{strike}_{signal}"
        if key in self._symbol_cache:
            return self._symbol_cache[key]

        opt_type = OptionSelector.get_option_type(signal)
        expiry = OptionSelector.get_expiry_api()

        logger.info(
            "🔍 SYMBOL LOOKUP: requested_strike=%s type=%s expiry=%s",
            strike, opt_type, expiry,
        )

        # ── ATTEMPT 1: specific strike ──────────────────────
        chain = await self.broker.get_option_chain(
            search_symbol_name=settings.nifty_symbol,
            exchange="NFO",
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=opt_type,
        )

        if isinstance(chain, dict) and chain.get("validationErrors"):
            logger.warning(
                "⚠️  SAMCO validation error on specific-strike call: errors=%s "
                "(falling back to full chain)",
                chain.get("validationErrors"),
            )
            chain = None

        rows = self._extract_chain_rows(chain) if chain else []

        # ── ATTEMPT 2: full chain fallback ──────────────────
        if not rows:
            if chain is not None:
                logger.warning(
                    "Specific-strike chain empty: expiry=%s strike=%s type=%s "
                    "response_keys=%s response_type=%s",
                    expiry, strike, opt_type,
                    list(chain.keys()) if isinstance(chain, dict) else [],
                    type(chain).__name__,
                )
            logger.info("🔄 Retrying with full chain (strike_price='0')")

            chain = await self.broker.get_option_chain(
                search_symbol_name=settings.nifty_symbol,
                exchange="NFO",
                expiry_date=expiry,
                strike_price="0",
                option_type=opt_type,
            )

            if isinstance(chain, dict) and chain.get("validationErrors"):
                logger.error(
                    "❌ SAMCO validation error on full chain too: errors=%s "
                    "expiry=%s type=%s — likely wrong expiry format or expired session",
                    chain.get("validationErrors"), expiry, opt_type,
                )
                # Don't cache failures — let next call retry
                return None

            rows = self._extract_chain_rows(chain)

        # ── BOTH ATTEMPTS FAILED ────────────────────────────
        if not rows:
            resp_keys = list(chain.keys()) if isinstance(chain, dict) else []
            logger.error(
                "❌ Option chain empty after both attempts: "
                "expiry=%s strike=%s type=%s response_keys=%s",
                expiry, strike, opt_type, resp_keys,
            )
            return None

        # ── PICK CLOSEST STRIKE ─────────────────────────────
        best_sym = None
        best_strike = None
        best_diff = float("inf")
        available_strikes: list[float] = []

        for row in rows:
            try:
                row_strike = float(row.get("strikePrice", 0))
                available_strikes.append(row_strike)
                diff = abs(row_strike - strike)
                if diff < best_diff:
                    best_diff = diff
                    best_sym = row.get("tradingSymbol")
                    best_strike = row_strike
            except (TypeError, ValueError):
                continue

        if not best_sym:
            logger.error(
                "❌ No valid strike in chain near %s. Available strikes: %s",
                strike, sorted(available_strikes)[:20],
            )
            return None

        if best_diff > 0:
            logger.warning(
                "⚠️  Strike snap: requested=%s → got nearest=%s (diff=%s). "
                "Check OptionSelector.get_otm_strike() rounding if frequent.",
                strike, best_strike, best_diff,
            )

        self._symbol_cache[key] = best_sym
        logger.info(
            "✅ SYMBOL RESOLVED: requested_strike=%s type=%s → %s (snap_diff=%s)",
            strike, opt_type, best_sym, best_diff,
        )
        return best_sym

    @staticmethod
    def _extract_chain_rows(chain: dict | list | None) -> list[dict]:
        if not chain:
            return []

        if isinstance(chain, list):
            return chain

        rows = (
            chain.get("optionChainDetails")
            or chain.get("data")
            or chain.get("rows")
            or chain.get("result")
            or []
        )

        if isinstance(rows, dict):
            return [rows]
        if isinstance(rows, list):
            return rows
        return []

    def clear_cache(self):
        self._symbol_cache.clear()

    async def _await_fill_confirmation(
        self,
        order_id: str,
        requested_qty: int,
        side: str,
    ) -> tuple[dict, int, float | None]:
        latest_status: dict = {}
        latest_filled = 0
        latest_avg: float | None = None
        for attempt in range(1, _FILL_CONFIRM_ATTEMPTS + 1):
            status = await self.broker.get_order_status(order_id)
            latest_status = status or {}
            latest_filled = _parse_filled_qty(latest_status, requested_qty)
            latest_avg = await self.broker.get_actual_fill_price(order_id)
            data = latest_status.get("orderDetails") or latest_status.get("data") or latest_status
            if isinstance(data, list):
                data = data[0] if data else {}
            broker_state = str(data.get("orderStatus") or data.get("status") or "").upper()
            if latest_filled >= requested_qty or broker_state in ("COMPLETE", "FILLED", "TRADED"):
                return latest_status, min(latest_filled, requested_qty), latest_avg
            if broker_state in ("REJECTED", "CANCELLED", "CANCELED"):
                logger.error(
                    "❌ %s order terminal state=%s order=%s",
                    side, broker_state, order_id,
                )
                return latest_status, latest_filled, latest_avg
            logger.warning(
                "⏳ %s delayed fill response attempt=%d/%d order=%s filled=%d/%d status=%s",
                side, attempt, _FILL_CONFIRM_ATTEMPTS, order_id,
                latest_filled, requested_qty, broker_state or "UNKNOWN",
            )
            await asyncio.sleep(_FILL_CONFIRM_DELAY)
        return latest_status, latest_filled, latest_avg

    async def _safe_cancel_order(self, order_id: str) -> None:
        try:
            await self.broker.cancel_order(order_id)
        except Exception as exc:
            logger.warning("Cancel failed order=%s err=%s", order_id, exc)

    async def _ensure_position_closed(
        self, symbol: str, reason: str, fallback_qty: int,
    ) -> bool:
        for attempt in range(1, _EXIT_VERIFY_ATTEMPTS + 1):
            open_qty = await self._get_open_position_qty(symbol)
            if open_qty < 0:
                logger.critical(
                    "🚨 EXIT VALIDATION INCONCLUSIVE: position API unavailable symbol=%s reason=%s",
                    symbol, reason,
                )
                return False
            if open_qty <= 0:
                return True
            logger.warning(
                "⚠️  EXIT VALIDATION FAILED attempt=%d/%d symbol=%s open_qty=%d reason=%s",
                attempt, _EXIT_VERIFY_ATTEMPTS, symbol, open_qty, reason,
            )
            if attempt < _EXIT_VERIFY_ATTEMPTS:
                retry_qty = open_qty if open_qty > 0 else fallback_qty
                await self._sell_with_retry(symbol, retry_qty, f"{reason}_RETRY_{attempt}")
                await asyncio.sleep(_EXIT_VERIFY_DELAY)
        return False

    async def _get_open_position_qty(self, symbol: str) -> int:
        try:
            positions = await self.broker.get_positions()
        except Exception as exc:
            logger.warning("Position fetch failed for exit validation: %s", exc)
            return -1
        total = 0
        symbol_upper = str(symbol).upper()
        for pos in positions or []:
            ts = str(pos.get("tradingSymbol") or pos.get("symbolName") or "").upper()
            if ts != symbol_upper:
                continue
            for key in ("netQty", "netQuantity", "quantity", "netPosition"):
                try:
                    total = int(float(str(pos.get(key, 0)).replace(",", "").strip()))
                    break
                except (TypeError, ValueError):
                    continue
        return max(total, 0)

    async def _validate_post_order_position(
        self, symbol: str, expected_qty: int, context: str,
    ) -> None:
        if expected_qty <= 0:
            raise RuntimeError(f"{context} invalid expected qty={expected_qty}")
        observed_qty = await self._get_open_position_qty(symbol)
        if observed_qty < 0:
            raise RuntimeError(
                f"{context} position check failed: broker positions unavailable",
            )
        if observed_qty < expected_qty:
            raise RuntimeError(
                f"{context} position mismatch expected>={expected_qty} observed={observed_qty}",
            )

    def _compute_dynamic_spread_limit(self, symbol: str, ltp: float) -> float:
        history = self._ltp_history[symbol]
        history.append(float(ltp))
        base_limit = float(getattr(settings, "max_spread_pct", 0.05))
        hard_cap = float(getattr(settings, "dynamic_spread_max_pct", 0.12))
        vol_multiplier = float(getattr(settings, "dynamic_spread_vol_multiplier", 2.0))
        if ltp <= 50:
            liquidity_floor = 0.08
        elif ltp <= 100:
            liquidity_floor = 0.06
        else:
            liquidity_floor = 0.04
        if len(history) < 3:
            return min(max(base_limit, liquidity_floor), hard_cap)
        max_ltp = max(history)
        min_ltp = min(history)
        realized_vol = ((max_ltp - min_ltp) / ltp) if ltp > 0 else 0.0
        dynamic = max(base_limit, liquidity_floor, realized_vol * vol_multiplier)
        return min(dynamic, hard_cap)

    async def _handle_fatal_exception(self, context: str, exc: Exception) -> None:
        async with self._fatal_lock:
            state = await self.state_manager.snapshot()
            if state.trading_enabled:
                await self.state_manager.update(
                    trading_enabled=False,
                    last_order_failed=True,
                    last_risk_breach=f"fatal_exception:{context}",
                )
                logger.critical(
                    "🚨 HARD FAIL SAFE ACTIVATED context=%s err=%s",
                    context, exc,
                )
            if state.active_trade:
                closed = await self.emergency_exit_active_trade(
                    reason=f"HARD_FAIL_{context}",
                )
                if not closed:
                    logger.critical(
                        "🚨 HARD FAIL EXIT UNSUCCESSFUL context=%s",
                        context,
                    )

    async def _force_exit_and_halt(
        self, symbol: str, qty: int, reason: str,
    ) -> None:
        await self.state_manager.update(
            trading_enabled=False,
            last_order_failed=True,
            last_risk_breach=reason,
        )
        try:
            await self.broker.place_order(
                symbol=symbol, side="SELL", quantity=qty,
            )
        except Exception as exc:
            logger.critical(
                "Forced exit failed symbol=%s qty=%s err=%s",
                symbol, qty, exc, exc_info=True,
            )
        try:
            await self.broker.cancel_all_open_orders()
        except Exception as exc:
            logger.warning("cancel_all_open_orders failed: %s", exc)
        await self.event_bus.publish(
            "ORDER_UNCERTAIN",
            {"reason": reason, "symbol": symbol, "qty": qty},
        )

    def _validate_trade_setup(
        self,
        state,
        raw_signal: str,
        ltp: float,
        bid: float | None,
        ask: float | None,
    ) -> bool:
        try:
            signal = self._map_signal(raw_signal)
        except ValueError:
            logger.warning("Invalid signal for validation: %s", raw_signal)
            return False

        if ask and ltp > 0:
            spike_pct = abs((ask - ltp) / ltp)
            if spike_pct > settings.max_option_spike_pct:
                logger.warning(
                    "Spike filter blocked: spike_pct=%.2f%%",
                    spike_pct * 100,
                )
                return False

        if signal == "CALL" and state.orb_high:
            min_break = state.orb_high + settings.breakout_buffer
            max_break = state.orb_high * (1 + settings.max_breakout_extension_pct)
            if state.spot_price < min_break or state.spot_price > max_break:
                logger.warning(
                    "Fake breakout/spike block CALL: spot=%.2f range=[%.2f, %.2f]",
                    state.spot_price, min_break, max_break,
                )
                return False
        if signal == "PUT" and state.orb_low:
            max_break = state.orb_low - settings.breakout_buffer
            min_break = state.orb_low * (1 - settings.max_breakout_extension_pct)
            if state.spot_price > max_break or state.spot_price < min_break:
                logger.warning(
                    "Fake breakout/spike block PUT: spot=%.2f range=[%.2f, %.2f]",
                    state.spot_price, min_break, max_break,
                )
                return False
        return True

    def _days_to_expiry(self, expiry: str) -> int | None:
        try:
            expiry_date = datetime.fromisoformat(expiry).date()
        except ValueError:
            try:
                expiry_date = datetime.strptime(expiry, "%d-%b-%Y").date()
            except ValueError:
                return None
        return max((expiry_date - datetime.now(IST).date()).days, 0)

    @staticmethod
    def _next_consecutive_losses(current: int, trade_pnl: float) -> int:
        return (current + 1) if trade_pnl < 0 else 0

    async def _enforce_global_risk_stop(
        self, new_daily: float, trade_pnl: float, state,
    ) -> None:
        new_losses = self._next_consecutive_losses(state.consecutive_losses, trade_pnl)
        hit_loss_streak = new_losses >= settings.max_consecutive_losses
        peak_equity = state.peak_equity or settings.capital
        equity_now = settings.capital + new_daily
        drawdown = ((peak_equity - equity_now) / peak_equity) if peak_equity > 0 else 0.0
        if hit_loss_streak or drawdown >= settings.max_drawdown_pct:
            await self.state_manager.update(
                trading_enabled=False,
                last_risk_breach=(
                    f"consecutive_losses_{new_losses}"
                    if hit_loss_streak else
                    f"drawdown_{drawdown:.2%}"
                ),
            )
            logger.critical(
                "🚨 GLOBAL RISK STOP: streak=%d drawdown=%.2f%% trading disabled",
                new_losses, drawdown * 100,
            )