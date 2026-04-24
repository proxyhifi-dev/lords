"""
Lords Bot — Trading Engine  v5.0
=================================
Listens to RISK_APPROVED → executes entry → monitors exit.

v5.0 fixes:
  1. entry_price = avgFillPrice (NOT ltp)  ← critical fix
  2. exit_price  = avgFillPrice (NOT ltp)  ← critical fix
  3. SELL retry up to 3 times with emergency market order fallback
  4. SL/T1/T2 levels recalculated from actual fill price
  5. Paper mode: falls back to LTP (no tradebook in paper)
  6. All existing v4.0 fixes preserved
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
_SELL_RETRY_DELAY = 1.5  # seconds between retries


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


class TradingEngine:

    def __init__(self, event_bus: EventBus, state_manager, trade_store: TradeStore, broker: SamcoClient):
        self.event_bus     = event_bus
        self.state_manager = state_manager
        self.trade_store   = trade_store
        self.broker        = broker
        self._trade_lock   = asyncio.Lock()
        self._symbol_cache: dict[str, str] = {}

    async def run(self):
        await asyncio.gather(
            self._entry_listener(),
            self._monitor_loop(),
            self._health_loop(),
        )

    # ── ENTRY ──────────────────────────────────────────────
    async def _entry_listener(self):
        queue = self.event_bus.subscribe("RISK_APPROVED")
        async for event in self.event_bus.iter_events(queue):
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

            signal     = payload.get("signal")
            size_label = payload.get("size_label", "FULL")

            try:
                strike = OptionSelector.get_otm_strike(
                    state.spot_price, signal, distance=settings.otm_distance)
                symbol = await self._resolve_symbol(strike, signal)
                if not symbol: return

                quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
                ltp   = self.broker.parse_ltp(quote)
                if not ltp:
                    logger.warning("LTP unavailable %s", symbol); return

                if ltp < settings.min_entry_premium:
                    logger.warning("Premium ₹%.1f < min ₹%.1f — skip %s",
                                   ltp, settings.min_entry_premium, symbol); return

                if settings.min_option_volume > 0:
                    vol = _parse_volume(quote)
                    if vol < settings.min_option_volume:
                        logger.warning("Low volume %d < %d — skip %s",
                                       vol, settings.min_option_volume, symbol); return

                qty = self._get_qty(size_label)

                # ── PLACE BUY + GET ACTUAL FILL PRICE ──────
                order_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol, side="BUY", quantity=qty
                )
                if not order_id:
                    logger.error("BUY order failed — no order_id"); return
                if fill_price is None and not order_id.startswith("PAPER-"):
                    logger.error("BUY fill not confirmed order=%s", order_id); return

                # Use actual fill price; fall back to LTP for paper mode
                entry_price = fill_price if fill_price else ltp
                logger.info("ENTRY fill price=₹%.2f (ltp=₹%.2f order=%s mode=%s)",
                            entry_price, ltp, order_id, settings.mode.upper())

                t1_qty = qty // 2
                t2_qty = qty - t1_qty
                trade  = {
                    "symbol":       symbol,
                    "strike":       strike,
                    "qty":          qty,
                    "entry_price":  entry_price,        # ← ACTUAL FILL PRICE
                    "entry_ltp":    ltp,                # ← saved for reference
                    "entry_time":   datetime.now(timezone.utc).isoformat(),
                    "status":       "OPEN",
                    "signal":       signal,
                    "max_price":    entry_price,
                    "order_id":     order_id,
                    "size_label":   size_label,
                    "sl_price":     round(entry_price * (1 - settings.stop_loss_pct), 2),
                    "t1_price":     round(entry_price * (1 + settings.t1_pct), 2),
                    "t2_price":     round(entry_price * (1 + settings.t2_pct), 2),
                    "t1_hit":       False,
                    "t1_booked":    False,
                    "t1_qty":       t1_qty,
                    "t2_qty":       t2_qty,
                    "t1_pnl":       0.0,
                }
                await self.state_manager.update(
                    active_trade=trade,
                    trade_count=state.trade_count + 1,
                )
                logger.info("TRADE OPEN %s qty=%s entry=₹%.2f SL=₹%.2f T1=₹%.2f T2=₹%.2f",
                            symbol, qty, entry_price,
                            trade["sl_price"], trade["t1_price"], trade["t2_price"])
                await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

            except Exception as exc:
                logger.error("Entry failed: %s", exc, exc_info=True)

    # ── MONITOR ────────────────────────────────────────────
    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(2)
            state = await self.state_manager.snapshot()
            trade = state.active_trade
            if not trade: continue

            try:
                quote = await self.broker.get_quote(symbol_name=trade["symbol"], exchange="NFO")
                ltp   = self.broker.parse_ltp(quote)
                if not ltp: continue

                entry     = trade["entry_price"]
                t1_booked = trade.get("t1_booked", False)
                sl_price  = trade.get("sl_price",  round(entry * (1 - settings.stop_loss_pct), 2))
                t1_price  = trade.get("t1_price",  round(entry * (1 + settings.t1_pct), 2))
                t2_price  = trade.get("t2_price",  round(entry * (1 + settings.t2_pct), 2))

                remaining_qty = trade.get("t2_qty", trade["qty"] // 2) if t1_booked else trade["qty"]
                live_pnl = (ltp - entry) * remaining_qty
                await self.state_manager.update(live_pnl=round(live_pnl, 2))

                if ltp > trade.get("max_price", ltp):
                    trade["max_price"] = ltp
                    await self.state_manager.update(active_trade=trade)

                trail_sl = round(trade["max_price"] * (1 - settings.trailing_pct), 2)

                sq_h, sq_m = map(int, settings.square_off.split(":"))
                if datetime.now(IST).time() >= dtime(sq_h, sq_m):
                    await self._exit_trade(trade, "EOD_SQUAREOFF", ltp); continue

                if ltp <= sl_price:
                    await self._exit_trade(trade, "STOPLOSS", ltp); continue

                if not t1_booked and ltp >= t1_price:
                    await self._book_partial(trade, ltp); continue

                if t1_booked:
                    if ltp >= t2_price:
                        await self._exit_remaining(trade, "TARGET_2", ltp); continue
                    if ltp <= trail_sl:
                        await self._exit_remaining(trade, "TRAIL_STOP", ltp); continue

            except Exception as exc:
                logger.error("Monitor loop: %s", exc, exc_info=True)

    # ── PARTIAL EXIT T1 ─────────────────────────────────────
    async def _book_partial(self, trade: dict, ltp: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade: return

            t1_qty = trade.get("t1_qty", trade["qty"] // 2)
            symbol = trade["symbol"]

            sell_id, fill_price = await self._sell_with_retry(symbol, t1_qty, "T1 PARTIAL")
            if not sell_id:
                logger.error("T1 SELL failed — keeping position open"); return

            # Use actual fill price; fallback to LTP
            exit_price = fill_price if fill_price else ltp
            t1_pnl = round((exit_price - trade["entry_price"]) * t1_qty, 2)

            trade["t1_hit"]    = True
            trade["t1_booked"] = True
            trade["t1_pnl"]    = t1_pnl

            new_daily = round(state.daily_pnl + t1_pnl, 2)
            await self.state_manager.update(
                active_trade=trade, daily_pnl=new_daily, live_pnl=0.0)
            logger.info("T1 BOOKED %s qty=%s fill=₹%.2f pnl=₹%.2f order=%s",
                        symbol, t1_qty, exit_price, t1_pnl, sell_id)
            await self.event_bus.publish("T1_BOOKED",
                {"symbol": symbol, "price": exit_price, "pnl": t1_pnl})

    # ── EXIT REMAINING (T2 / trail) ─────────────────────────
    async def _exit_remaining(self, trade: dict, reason: str, ltp: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade: return

            symbol = trade["symbol"]
            t2_qty = trade.get("t2_qty", trade["qty"] // 2)

            sell_id, fill_price = await self._sell_with_retry(symbol, t2_qty, reason)
            if not sell_id:
                logger.critical("T2 SELL FAILED — position may be open! %s qty=%s", symbol, t2_qty)
                await self.event_bus.publish("SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": t2_qty, "reason": reason})
                return

            exit_price = fill_price if fill_price else ltp
            t2_pnl     = round((exit_price - trade["entry_price"]) * t2_qty, 2)
            total_pnl  = round(trade.get("t1_pnl", 0) + t2_pnl, 2)
            new_daily  = round(state.daily_pnl + t2_pnl, 2)

            closed = {**trade, "exit_price": exit_price,
                      "exit_time": datetime.now(timezone.utc).isoformat(),
                      "status": "CLOSED", "exit_reason": reason,
                      "pnl": total_pnl, "sell_order_id": sell_id}
            self.trade_store.append_trade(closed, new_daily)
            await self.state_manager.update(active_trade=None, daily_pnl=new_daily, live_pnl=0.0)
            logger.info("EXIT %s reason=%s fill=₹%.2f pnl=₹%.2f", symbol, reason, exit_price, total_pnl)
            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    # ── FULL EXIT (SL / EOD) ────────────────────────────────
    async def _exit_trade(self, trade: dict, reason: str, ltp: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade: return

            symbol = trade["symbol"]
            qty    = trade.get("t2_qty", trade["qty"] // 2) if trade.get("t1_booked") else trade["qty"]

            sell_id, fill_price = await self._sell_with_retry(symbol, qty, reason)
            if not sell_id:
                logger.critical("SELL FAILED — position may be open! %s qty=%s reason=%s",
                                symbol, qty, reason)
                await self.event_bus.publish("SELL_FAILED_CRITICAL",
                    {"symbol": symbol, "qty": qty, "reason": reason})
                return

            exit_price = fill_price if fill_price else ltp
            exit_pnl   = round((exit_price - trade["entry_price"]) * qty, 2)
            total_pnl  = round(trade.get("t1_pnl", 0) + exit_pnl, 2)
            new_daily  = round(state.daily_pnl + exit_pnl, 2)

            closed = {**trade, "exit_price": exit_price,
                      "exit_time": datetime.now(timezone.utc).isoformat(),
                      "status": "CLOSED", "exit_reason": reason,
                      "pnl": total_pnl, "sell_order_id": sell_id}
            self.trade_store.append_trade(closed, new_daily)
            await self.state_manager.update(active_trade=None, daily_pnl=new_daily, live_pnl=0.0)
            logger.info("EXIT %s reason=%s fill=₹%.2f pnl=₹%.2f", symbol, reason, exit_price, total_pnl)
            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    # ── SELL WITH RETRY ─────────────────────────────────────
    async def _sell_with_retry(
        self, symbol: str, qty: int, reason: str
    ) -> tuple[str | None, float | None]:
        """
        Try SELL up to _SELL_MAX_RETRIES times.
        If all retries fail → emergency market order with 1s delay.
        Returns (order_id, fill_price) or (None, None) if all fail.
        """
        for attempt in range(1, _SELL_MAX_RETRIES + 1):
            try:
                sell_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol, side="SELL", quantity=qty
                )
                if sell_id:
                    logger.info("SELL OK attempt=%d reason=%s order=%s fill=₹%s",
                                attempt, reason, sell_id,
                                f"{fill_price:.2f}" if fill_price else "N/A(paper)")
                    return sell_id, fill_price
                logger.warning("SELL attempt %d/%d failed — no order_id reason=%s",
                               attempt, _SELL_MAX_RETRIES, reason)
            except Exception as exc:
                logger.error("SELL attempt %d/%d exception reason=%s: %s",
                             attempt, _SELL_MAX_RETRIES, reason, exc)

            if attempt < _SELL_MAX_RETRIES:
                await asyncio.sleep(_SELL_RETRY_DELAY)

        # Emergency: last-resort market order
        logger.critical("EMERGENCY SELL: all retries failed — sending emergency order %s qty=%s",
                        symbol, qty)
        try:
            resp = await self.broker.place_order(symbol=symbol, side="SELL", quantity=qty)
            eid  = resp.get("orderNumber") or resp.get("orderId")
            if eid:
                await asyncio.sleep(2)  # wait 2s then fetch fill
                fp = await self.broker.get_actual_fill_price(eid)
                logger.critical("EMERGENCY SELL placed order=%s fill=%s", eid, fp)
                return eid, fp
        except Exception as exc:
            logger.critical("EMERGENCY SELL also failed: %s", exc)

        return None, None

    # ── HEALTH ──────────────────────────────────────────────
    async def _health_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                if not await self.broker.healthcheck():
                    logger.warning("Healthcheck failed — re-login")
                    await self.broker.login()
            except Exception as exc:
                logger.error("Health loop: %s", exc)

    # ── HELPERS ─────────────────────────────────────────────
    def _get_qty(self, size_label: str) -> int:
        base = settings.order_qty
        if size_label == "FULL":   return max(base, 1)
        if size_label == "MEDIUM": return max(int(round(base * 0.75)), 1)
        if size_label == "HALF":   return max(int(round(base * 0.50)), 1)
        return base

    async def _resolve_symbol(self, strike: int, signal: str) -> str | None:
        key = f"{strike}_{signal}"
        if key in self._symbol_cache: return self._symbol_cache[key]

        opt_type = OptionSelector.get_option_type(signal)
        expiry   = OptionSelector.get_expiry_api()
        chain    = await self.broker.get_option_chain(
            search_symbol_name="NIFTY", exchange="NFO",
            expiry_date=expiry, strike_price=str(strike), option_type=opt_type,
        )
        rows = chain.get("optionChainDetails") or chain.get("data") or []
        if not rows:
            logger.error("Option chain empty expiry=%s strike=%s type=%s", expiry, strike, opt_type)
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

    def clear_cache(self):
        self._symbol_cache.clear()
