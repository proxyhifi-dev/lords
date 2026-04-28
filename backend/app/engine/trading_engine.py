"""
Lords Bot — Trading Engine v5.2 (FIXED WITH STRATEGY INTEGRATION)
==================================================================
Listens to RISK_APPROVED → executes entry → monitors exit.

✅ v5.2 CRITICAL FIX:
  1. Added strategy parameter to __init__
  2. Call strategy.set_already_traded_today() AFTER successful entry
  3. Prevents overtrading (max 1 trade/day)
  4. Prevents timing bug (flag set only after success, not before)

Based on v5.1 which includes:
  1. Partial fill handling — detects qty filled < qty ordered
  2. entry_price = avgFillPrice (NOT ltp)
  3. exit_price  = avgFillPrice (NOT ltp)
  4. SELL retry up to 3 times with emergency market order fallback
  5. SL/T1/T2 levels from actual fill price
  6. Paper mode falls back to LTP (no tradebook in paper)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("trading_engine")
IST = ZoneInfo("Asia/Kolkata")

_SELL_MAX_RETRIES = 3
_SELL_RETRY_DELAY = 1.5   # seconds between retries
_FILL_CONFIRM_ATTEMPTS = 8
_FILL_CONFIRM_DELAY = 0.75
_EXIT_VERIFY_ATTEMPTS = 4
_EXIT_VERIFY_DELAY = 1.0


def _parse_volume(quote: dict) -> int:
    def _int(val) -> int:
        try: return int(float(str(val).replace(",", "").strip()))
        except (TypeError, ValueError): return 0
    for k in ("tradedVolume", "volume", "traded_volume", "totalTradedVolume"):
        v = _int(quote.get(k))
        if v > 0: return v
    inner = quote.get("quoteDetails")
    if isinstance(inner, list) and inner: inner = inner[0]
    if isinstance(inner, dict):
        for k in ("tradedVolume", "volume", "totalTradedVolume"):
            v = _int(inner.get(k))
            if v > 0: return v
    data = quote.get("data")
    if isinstance(data, list) and data: data = data[0]
    if isinstance(data, dict):
        for k in ("tradedVolume", "volume", "totalTradedVolume"):
            v = _int(data.get(k))
            if v > 0: return v
    return 0


def _parse_filled_qty(order_status: dict, requested_qty: int) -> int:
    """
    Extract actual filled quantity from SAMCO order status response.
    Returns 0 when not available and status is non-terminal.
    """
    def _int(val) -> int:
        try: return int(float(str(val).replace(",", "").strip()))
        except (TypeError, ValueError): return 0

    data = order_status.get("orderDetails") or order_status.get("data") or order_status
    if isinstance(data, list): data = data[0] if data else {}

    for key in ("filledShares", "tradedQty", "filledQty",
                "executedQty", "filled_quantity", "tradedQuantity"):
        v = _int(data.get(key))
        if v > 0: return v

    # If status is COMPLETE and no partial qty field, assume full fill
    status = str(data.get("orderStatus") or data.get("status") or "").upper()
    if status in ("COMPLETE", "FILLED", "TRADED"):
        return requested_qty

    return 0


class TradingEngine:

    def __init__(self, event_bus: EventBus, state_manager,
                 trade_store: TradeStore, broker: SamcoClient, strategy=None):
        """
        ✅ CRITICAL: Added strategy parameter for daily limit integration
        """
        self.event_bus     = event_bus
        self.state_manager = state_manager
        self.trade_store   = trade_store
        self.broker        = broker
        self.strategy      = strategy  # ✅ NEW: Store strategy reference
        self._trade_lock   = asyncio.Lock()
        self._symbol_cache: dict[str, str] = {}
        self._fatal_lock = asyncio.Lock()

    def _map_signal(self, raw_signal: str) -> str:
        """Map trading signals to option types."""
        if raw_signal == "LONG":
            return "CALL"
        elif raw_signal == "SHORT":
            return "PUT"
        else:
            raise ValueError(f"Invalid signal: {raw_signal}. Expected LONG or SHORT.")

    async def run(self):
        logger.info("TradingEngine started")
        await asyncio.gather(
            self._entry_listener(),
            self._monitor_loop(),
            self._health_loop(),
        )

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

            if state.active_trade: return
            if state.spot_price is None: return
            if not state.trading_enabled: return

            no_h, no_m = map(int, settings.no_entry_after.split(":"))
            if datetime.now(IST).time() >= dtime(no_h, no_m):
                logger.info("Past no-entry time — skipping"); return

            # 🔍 LOG: Signal received
            raw_signal = payload.get("signal")
            size_label = payload.get("size_label", "FULL")
            logger.info("📡 SIGNAL RECEIVED: raw='%s' size='%s' spot=₹%.2f",
                       raw_signal, size_label, state.spot_price or 0)

            try:
                # 🔄 Map signal to option type
                signal = self._map_signal(raw_signal)
                logger.info("🔄 SIGNAL MAPPED: '%s' → '%s'", raw_signal, signal)

                # 🎯 Calculate strike
                strike = OptionSelector.get_otm_strike(
                    state.spot_price, signal, distance=settings.otm_distance)
                logger.info("🎯 STRIKE CALCULATED: spot=₹%.2f signal='%s' → strike=%d",
                           state.spot_price, signal, strike)

                # 🔍 Resolve symbol
                symbol = await self._resolve_symbol(strike, signal)
                if not symbol:
                    logger.error("❌ SYMBOL RESOLUTION FAILED: strike=%d signal='%s'",
                                strike, signal)
                    return
                logger.info("✅ SYMBOL RESOLVED: %s", symbol)

                # 📊 Get quote and LTP
                quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
                ltp   = self.broker.parse_ltp(quote)
                bid, ask = self.broker.parse_bid_ask(quote)
                if not ltp:
                    logger.warning("❌ LTP UNAVAILABLE: %s", symbol)
                    return
                logger.info("💰 LTP FETCHED: %s = ₹%.2f", symbol, ltp)

                if bid and ask and ask > bid:
                    spread = ask - bid
                    spread_pct = spread / ask
                    max_spread_pct = getattr(settings, "max_spread_pct", 0.05)
                    if spread_pct > max_spread_pct:
                        logger.warning(
                            "❌ SPREAD TOO WIDE: %.2f%% > %.2f%% symbol=%s bid=%.2f ask=%.2f",
                            spread_pct * 100, max_spread_pct * 100, symbol, bid, ask
                        )
                        return

                if not self._validate_trade_setup(state, raw_signal, ltp, bid, ask):
                    logger.warning("❌ TRADE VALIDATION FAILED: symbol=%s signal=%s", symbol, raw_signal)
                    return

                # 🛡️ Premium check
                if ltp < settings.min_entry_premium:
                    logger.warning("❌ PREMIUM TOO LOW: ₹%.1f < min ₹%.1f — skip %s",
                                   ltp, settings.min_entry_premium, symbol)
                    return

                # 📈 Volume check
                if settings.min_option_volume > 0:
                    vol = _parse_volume(quote)
                    if vol < settings.min_option_volume:
                        logger.warning("❌ LOW VOLUME: %d < %d — skip %s",
                                       vol, settings.min_option_volume, symbol)
                        return
                    logger.info("📊 VOLUME OK: %s = %d", symbol, vol)

                requested_qty = self._get_qty(size_label)
                logger.info("📋 QTY CALCULATED: size='%s' → qty=%d", size_label, requested_qty)

                # 🛒 PLACE BUY ORDER
                logger.info("🛒 PLACING BUY ORDER: %s qty=%d", symbol, requested_qty)
                order_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol, side="BUY", quantity=requested_qty
                )
                if not order_id:
                    logger.error("❌ BUY ORDER FAILED: no order_id for %s", symbol)
                    return

                # 💰 FILL CONFIRMATION
                is_paper = order_id.startswith("PAPER-")
                if fill_price is None and not is_paper:
                    logger.error("❌ BUY FILL NOT CONFIRMED: order=%s", order_id)
                    return

                conservative_ltp = ask if ask else ltp
                entry_price = fill_price if fill_price else conservative_ltp
                logger.info("💰 BUY FILL CONFIRMED: order=%s fill=₹%.2f ltp=₹%.2f mode=%s",
                           order_id, entry_price, ltp, settings.mode.upper())

                # 🔍 PARTIAL FILL DETECTION
                filled_qty = requested_qty  # default: full fill
                if not is_paper:
                    try:
                        order_status, filled_qty, broker_avg = await self._await_fill_confirmation(
                            order_id=order_id,
                            requested_qty=requested_qty,
                            side="BUY",
                        )
                        if broker_avg and not fill_price:
                            fill_price = broker_avg
                        if filled_qty < requested_qty:
                            logger.warning(
                                "⚠️  PARTIAL FILL DETECTED: requested=%d filled=%d symbol=%s",
                                requested_qty, filled_qty, symbol
                            )
                            remaining = requested_qty - filled_qty
                            if remaining > 0:
                                logger.warning(
                                    "⚠️ BUY PARTIAL REMAINING: cancelling unfilled qty=%d order=%s",
                                    remaining, order_id
                                )
                                await self._safe_cancel_order(order_id)
                        else:
                            logger.info("✅ FULL FILL CONFIRMED: qty=%d", filled_qty)
                    except Exception as exc:
                        logger.warning("⚠️  COULD NOT VERIFY FILLED QTY: %s — assuming full fill", exc)

                logger.info(
                    "📈 ENTRY SUMMARY: fill=₹%.2f ltp=₹%.2f qty=%d/%d order=%s mode=%s",
                    entry_price, ltp, filled_qty, requested_qty,
                    order_id, settings.mode.upper()
                )

                # 🎯 CREATE TRADE OBJECT
                t1_qty = filled_qty // 2
                t2_qty = filled_qty - t1_qty

                trade = {
                    "symbol":          symbol,
                    "strike":          strike,
                    "qty":             filled_qty,        # ← ACTUAL filled qty
                    "requested_qty":   requested_qty,     # ← original request
                    "entry_price":     entry_price,       # ← ACTUAL fill price
                    "entry_ltp":       ltp,               # ← LTP at signal time
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
                }

                # 💾 SAVE TRADE TO STATE
                await self.state_manager.update(
                    active_trade=trade,
                    trade_count=state.trade_count + 1,
                )

                # 🎉 TRADE OPEN LOG
                logger.info(
                    "🚀 TRADE OPENED: %s qty=%d entry=₹%.2f SL=₹%.2f T1=₹%.2f T2=₹%.2f%s",
                    symbol, filled_qty, entry_price,
                    trade["sl_price"], trade["t1_price"], trade["t2_price"],
                    " [PARTIAL FILL]" if trade["partial_fill"] else ""
                )
                logger.info(
                    "📊 TRADE DETAILS: strike=%d signal='%s' t1_qty=%d t2_qty=%d order=%s",
                    strike, signal, t1_qty, t2_qty, order_id
                )

                # ✅ CRITICAL FIX: Set daily limit ONLY AFTER successful entry
                if self.strategy:
                    self.strategy.set_already_traded_today()
                    logger.info("🔒 Daily trade limit locked (after successful entry)")

                await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

            except Exception as exc:
                logger.error("❌ ENTRY FAILED: %s", exc, exc_info=True)
                await self._handle_fatal_exception("entry", exc)
    # ── MONITOR ─────────────────────────────
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
                    symbol_name=trade["symbol"], exchange="NFO"
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

                if ltp > trade.get("max_price", entry):
                    trade["max_price"] = ltp
                    await self.state_manager.update(active_trade=trade)

                trail_sl = round(
                    trade["max_price"] * (1 - settings.trailing_pct), 2
                )

                sq_h, sq_m = map(int, settings.square_off.split(":"))

                if datetime.now(IST).time() >= dtime(sq_h, sq_m):
                    await self._exit_trade(trade, "EOD_SQUAREOFF", ltp)
                    continue

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

            logger.info("🔄 BOOKING PARTIAL: %s qty=%d ltp=₹%.2f",
                       symbol, t1_qty, ltp)

            sell_id, fill_price = await self._sell_with_retry(
                symbol, t1_qty, "TARGET_1"
            )
            if not sell_id:
                logger.critical(
                    "❌ T1 SELL FAILED — position may be open! %s qty=%d",
                    symbol, t1_qty
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t1_qty, "reason": "TARGET_1"}
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            t1_pnl = round((exit_price - trade["entry_price"]) * t1_qty, 2)

            logger.info("💰 T1 PARTIAL FILL: order=%s fill=₹%.2f pnl=₹%.2f",
                       sell_id, exit_price, t1_pnl)

            trade["t1_booked"] = True
            trade["t1_pnl"] = t1_pnl
            trade["t1_exit_price"] = exit_price

            await self.state_manager.update(active_trade=trade)

            logger.info(
                "✅ PARTIAL BOOKED: %s t1_pnl=₹%.2f remaining_qty=%d",
                symbol, t1_pnl, trade.get("t2_qty", trade["qty"] // 2)
            )

            await self.event_bus.publish("PARTIAL_BOOKED", {"trade": trade})

     # ── EXIT REMAINING — T2 / Trail ──────────────────────────
    async def _exit_remaining(self, trade: dict, reason: str, ltp: float):

        # ✅ prevent double sell
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

            logger.info("🔄 EXITING REMAINING: %s qty=%d reason='%s' ltp=₹%.2f",
                       symbol, t2_qty, reason, ltp)

            sell_id, fill_price = await self._sell_with_retry(
                symbol, t2_qty, reason
            )
            if not sell_id:
                logger.critical(
                    "❌ T2 SELL FAILED — position may be open! %s qty=%d reason=%s",
                    symbol, t2_qty, reason
                )
                await self.event_bus.publish(
                    "SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t2_qty, "reason": reason}
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

            logger.info("💰 T2 EXIT FILL: order=%s fill=₹%.2f pnl=₹%.2f total_pnl=₹%.2f",
                       sell_id, exit_price, t2_pnl, total_pnl)
            position_closed = await self._ensure_position_closed(symbol, reason, fallback_qty=t2_qty)
            if not position_closed:
                await self._handle_fatal_exception(
                    f"exit_remaining_verify_failed:{reason}",
                    RuntimeError("Broker position still open after exit"),
                )
                return

            # ✅ mark closed BEFORE saving
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
                    state.consecutive_losses, total_pnl
                ),
            )
            await self._enforce_global_risk_stop(new_daily, total_pnl, state)

            logger.info(
                "✅ TRADE CLOSED (REMAINING): %s reason='%s' exit=₹%.2f pnl=₹%.2f daily_pnl=₹%.2f",
                symbol, reason, exit_price, total_pnl, new_daily
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})
    # ── FULL EXIT — SL / EOD ─────────────────────────────────
    async def _exit_trade(self, trade: dict, reason: str, ltp: float):

        # ✅ CRITICAL: prevent double sell
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

            logger.info("🔄 EXITING TRADE: %s qty=%d reason='%s' ltp=₹%.2f",
                       symbol, qty, reason, ltp)

            sell_id, fill_price = await self._sell_with_retry(symbol, qty, reason)
            if not sell_id:
                logger.critical(
                    "❌ SELL FAILED — position may be open! %s qty=%d reason=%s",
                    symbol, qty, reason)
                await self.event_bus.publish("SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": qty, "reason": reason})
                await self._handle_fatal_exception(
                    f"exit_trade_sell_failed:{reason}",
                    RuntimeError("Full exit sell failed"),
                )
                return

            quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
            bid, _ = self.broker.parse_bid_ask(quote)
            conservative_ltp = bid if bid else ltp
            exit_price = fill_price if fill_price else conservative_ltp
            exit_pnl   = round((exit_price - trade["entry_price"]) * qty, 2)
            total_pnl  = round(trade.get("t1_pnl", 0) + exit_pnl, 2)
            new_daily  = round(state.daily_pnl + exit_pnl, 2)

            logger.info("💰 EXIT FILL: order=%s fill=₹%.2f pnl=₹%.2f total_pnl=₹%.2f",
                       sell_id, exit_price, exit_pnl, total_pnl)
            position_closed = await self._ensure_position_closed(symbol, reason, fallback_qty=qty)
            if not position_closed:
                await self._handle_fatal_exception(
                    f"exit_trade_verify_failed:{reason}",
                    RuntimeError("Broker position still open after full exit"),
                )
                return

            # ✅ mark CLOSED BEFORE saving
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
                    state.consecutive_losses, total_pnl
                ),
            )
            await self._enforce_global_risk_stop(new_daily, total_pnl, state)

            logger.info(
                "✅ TRADE CLOSED: %s reason='%s' exit=₹%.2f pnl=₹%.2f daily_pnl=₹%.2f",
                symbol, reason, exit_price, total_pnl, new_daily
            )

            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    # ── SELL WITH RETRY ──────────────────────────────────────
    async def _sell_with_retry(
        self, symbol: str, qty: int, reason: str
    ) -> tuple[str | None, float | None]:
        """
        Try SELL up to _SELL_MAX_RETRIES times.
        After all retries fail → emergency market order.
        Returns (order_id, fill_price) or (None, None) if everything fails.
        """
        for attempt in range(1, _SELL_MAX_RETRIES + 1):
            try:
                sell_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol, side="SELL", quantity=qty
                )
                if sell_id:
                    logger.info(
                        "SELL OK attempt=%d reason=%s order=%s fill=%s",
                        attempt, reason, sell_id,
                        f"₹{fill_price:.2f}" if fill_price else "N/A(paper)"
                    )
                    return sell_id, fill_price
                logger.warning(
                    "SELL attempt %d/%d no order_id reason=%s",
                    attempt, _SELL_MAX_RETRIES, reason)
            except Exception as exc:
                logger.error(
                    "SELL attempt %d/%d exception reason=%s: %s",
                    attempt, _SELL_MAX_RETRIES, reason, exc)

            if attempt < _SELL_MAX_RETRIES:
                await asyncio.sleep(_SELL_RETRY_DELAY)

        # Emergency fallback — direct market order
        logger.critical(
            "EMERGENCY SELL: all %d retries failed — "
            "placing emergency order %s qty=%d",
            _SELL_MAX_RETRIES, symbol, qty)
        try:
            resp = await self.broker.place_order(
                symbol=symbol, side="SELL", quantity=qty)
            eid = resp.get("orderNumber") or resp.get("orderId")
            if eid:
                await asyncio.sleep(2)
                fp = await self.broker.get_actual_fill_price(eid)
                logger.critical(
                    "EMERGENCY SELL placed order=%s fill=%s", eid, fp)
                return eid, fp
        except Exception as exc:
            logger.critical("EMERGENCY SELL also failed: %s", exc)

        return None, None

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
            except Exception as exc:
                logger.error("Health loop: %s", exc)

    # ── HELPERS ──────────────────────────────────────────────
    def _get_qty(self, size_label: str) -> int:
        base = settings.order_qty
        if size_label == "FULL":   return max(base, 1)
        if size_label == "MEDIUM": return max(int(round(base * 0.75)), 1)
        if size_label == "HALF":   return max(int(round(base * 0.50)), 1)
        return base

    async def _resolve_symbol(self, strike: int, signal: str) -> str | None:
        key = f"{strike}_{signal}"
        if key in self._symbol_cache:
            return self._symbol_cache[key]

        opt_type = OptionSelector.get_option_type(signal)
        expiry   = OptionSelector.get_expiry_api()

        chain = await self.broker.get_option_chain(
            search_symbol_name=settings.nifty_symbol,
            exchange="NFO",
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=opt_type,
        )
        rows = self._extract_chain_rows(chain)

        if not rows:
            logger.warning(
                "Exact strike chain empty expiry=%s strike=%s type=%s, retrying full chain",
                expiry, strike, opt_type)
            chain = await self.broker.get_option_chain(
                search_symbol_name=settings.nifty_symbol,
                exchange="NFO",
                expiry_date=expiry,
                strike_price="0",
                option_type=opt_type,
            )
            rows = self._extract_chain_rows(chain)

        if not rows:
            resp_type = type(chain).__name__
            resp_keys = list(chain.keys()) if isinstance(chain, dict) else []
            logger.error(
                "Option chain empty expiry=%s strike=%s type=%s response=%s keys=%s",
                expiry, strike, opt_type, resp_type, resp_keys)
            return None

        best_sym, best_diff = None, 999_999.0
        for row in rows:
            diff = abs(float(row.get("strikePrice", 0)) - strike)
            if diff < best_diff:
                best_diff = diff
                best_sym  = row.get("tradingSymbol")

        if best_sym:
            self._symbol_cache[key] = best_sym
            logger.info("Resolved %s %s → %s", strike, opt_type, best_sym)
        return best_sym

    @staticmethod
    def _extract_chain_rows(chain: dict | list | None) -> list[dict]:
        if not chain:
            return []

        if isinstance(chain, list):
            return chain

        rows = (chain.get("optionChainDetails")
                or chain.get("data")
                or chain.get("rows")
                or chain.get("result")
                or [])

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
                logger.error("❌ %s order terminal state=%s order=%s", side, broker_state, order_id)
                return latest_status, latest_filled, latest_avg
            logger.warning(
                "⏳ %s delayed fill response attempt=%d/%d order=%s filled=%d/%d status=%s",
                side, attempt, _FILL_CONFIRM_ATTEMPTS, order_id, latest_filled, requested_qty, broker_state or "UNKNOWN"
            )
            await asyncio.sleep(_FILL_CONFIRM_DELAY)
        return latest_status, latest_filled, latest_avg

    async def _safe_cancel_order(self, order_id: str) -> None:
        try:
            await self.broker.cancel_order(order_id)
        except Exception as exc:
            logger.warning("Cancel failed order=%s err=%s", order_id, exc)

    async def _ensure_position_closed(self, symbol: str, reason: str, fallback_qty: int) -> bool:
        for attempt in range(1, _EXIT_VERIFY_ATTEMPTS + 1):
            open_qty = await self._get_open_position_qty(symbol)
            if open_qty <= 0:
                return True
            logger.warning(
                "⚠️ EXIT VALIDATION FAILED attempt=%d/%d symbol=%s open_qty=%d reason=%s",
                attempt, _EXIT_VERIFY_ATTEMPTS, symbol, open_qty, reason
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
            return 0
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

    async def _handle_fatal_exception(self, context: str, exc: Exception) -> None:
        async with self._fatal_lock:
            state = await self.state_manager.snapshot()
            if state.trading_enabled:
                await self.state_manager.update(
                    trading_enabled=False,
                    last_order_failed=True,
                    last_risk_breach=f"fatal_exception:{context}",
                )
                logger.critical("🚨 HARD FAIL SAFE ACTIVATED context=%s err=%s", context, exc)
            if state.active_trade:
                closed = await self.emergency_exit_active_trade(reason=f"HARD_FAIL_{context}")
                if not closed:
                    logger.critical("🚨 HARD FAIL EXIT UNSUCCESSFUL context=%s", context)

    def _validate_trade_setup(
        self,
        state,
        raw_signal: str,
        ltp: float,
        bid: float | None,
        ask: float | None,
    ) -> bool:
        if ask and ltp > 0:
            spike_pct = abs((ask - ltp) / ltp)
            if spike_pct > settings.max_option_spike_pct:
                logger.warning("Spike filter blocked: spike_pct=%.2f%%", spike_pct * 100)
                return False
        if raw_signal == "LONG" and state.orb_high:
            min_break = state.orb_high + settings.breakout_buffer
            max_break = state.orb_high * (1 + settings.max_breakout_extension_pct)
            if state.spot_price < min_break or state.spot_price > max_break:
                logger.warning("Fake breakout/spike block LONG: spot=%.2f range=[%.2f, %.2f]",
                               state.spot_price, min_break, max_break)
                return False
        if raw_signal == "SHORT" and state.orb_low:
            max_break = state.orb_low - settings.breakout_buffer
            min_break = state.orb_low * (1 - settings.max_breakout_extension_pct)
            if state.spot_price > max_break or state.spot_price < min_break:
                logger.warning("Fake breakout/spike block SHORT: spot=%.2f range=[%.2f, %.2f]",
                               state.spot_price, min_break, max_break)
                return False
        return True

    @staticmethod
    def _next_consecutive_losses(current: int, trade_pnl: float) -> int:
        return (current + 1) if trade_pnl < 0 else 0

    async def _enforce_global_risk_stop(self, new_daily: float, trade_pnl: float, state) -> None:
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
                new_losses, drawdown * 100
            )
