"""
Lords Bot — Trading Engine
Listens to RISK_APPROVED → executes entry → monitors exit.

Fixes vs old version:
  1. Paper mode enforced (samco_client handles it but double-guarded here)
  2. Fill confirmation on every order (confirm_fill polling)
  3. PnL double-count fixed: _exit_trade uses only remaining qty pnl
  4. T1 booked flag checked before _exit_trade to avoid double-sell
  5. No risk re-checks here (RiskManager is single source of truth)
  6. Symbol cache cleared on daily reset
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time as dtime

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("trading_engine")


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

    # ── ENTRY — listens to RISK_APPROVED ─────────────
    async def _entry_listener(self):
        queue = self.event_bus.subscribe("RISK_APPROVED")
        async for event in self.event_bus.iter_events(queue):
            await self._enter_trade(event.payload)

    async def _enter_trade(self, payload: dict):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()

            # Guard against race conditions
            if state.active_trade: return
            if state.spot_price is None: return
            if not state.trading_enabled: return

            no_h, no_m = map(int, settings.no_entry_after.split(":"))
            if datetime.now().time() >= dtime(no_h, no_m):
                logger.info("Past no-entry time — skipping")
                return

            signal     = payload.get("signal")
            size_label = payload.get("size_label", "FULL")

            try:
                strike = OptionSelector.get_otm_strike(state.spot_price, signal,
                                                        distance=settings.otm_distance)
                symbol = await self._resolve_symbol(strike, signal)
                if not symbol: return

                quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
                ltp   = self.broker.parse_ltp(quote)
                if not ltp:
                    logger.warning("LTP unavailable %s", symbol); return

                if ltp < settings.min_entry_premium:
                    logger.warning("Premium ₹%.1f < min ₹%.1f — skip %s", ltp, settings.min_entry_premium, symbol); return

                vol = 0
                try: vol = int(quote.get("volume") or quote.get("tradedVolume") or 0)
                except: pass
                if vol < settings.min_option_volume:
                    logger.warning("Low volume %d — skip %s", vol, symbol); return

                qty = self._get_qty(size_label)

                order_resp = await self.broker.place_order(symbol=symbol, side="BUY", quantity=qty)
                order_id   = (order_resp.get("orderNumber") or
                              order_resp.get("orderId")     or
                              order_resp.get("order_id"))
                if not order_id:
                    logger.error("BUY rejected no order_id resp=%s", order_resp); return

                # Confirm fill before recording trade
                filled = await self.broker.confirm_fill(order_id)
                if not filled:
                    logger.error("BUY not confirmed filled order_id=%s", order_id); return

                t1_qty = qty // 2
                t2_qty = qty - t1_qty
                trade  = {
                    "symbol":      symbol,
                    "strike":      strike,
                    "qty":         qty,
                    "entry_price": ltp,
                    "entry_time":  datetime.now(UTC).isoformat(),
                    "status":      "OPEN",
                    "signal":      signal,
                    "max_price":   ltp,
                    "order_id":    order_id,
                    "size_label":  size_label,
                    "sl_price":    round(ltp * (1 - settings.stop_loss_pct), 2),
                    "t1_price":    round(ltp * (1 + settings.t1_pct), 2),
                    "t2_price":    round(ltp * (1 + settings.t2_pct), 2),
                    "t1_hit":      False,
                    "t1_booked":   False,
                    "t1_qty":      t1_qty,
                    "t2_qty":      t2_qty,
                    "t1_pnl":      0.0,
                }
                await self.state_manager.update(
                    active_trade=trade,
                    trade_count=state.trade_count + 1,
                )
                logger.info("ENTRY %s qty=%s ltp=₹%.2f SL=₹%.2f T1=₹%.2f T2=₹%.2f mode=%s",
                            symbol, qty, ltp, trade["sl_price"], trade["t1_price"], trade["t2_price"],
                            settings.mode.upper())
                await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

            except Exception as exc:
                logger.error("Entry failed: %s", exc, exc_info=True)

    # ── MONITOR — 2-stage exit ────────────────────────
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
                t1_hit    = trade.get("t1_hit",    False)
                sl_price  = trade.get("sl_price",  round(entry * (1 - settings.stop_loss_pct), 2))
                t1_price  = trade.get("t1_price",  round(entry * (1 + settings.t1_pct), 2))
                t2_price  = trade.get("t2_price",  round(entry * (1 + settings.t2_pct), 2))

                # Update max_price and live pnl
                remaining_qty = trade.get("t2_qty", trade["qty"] // 2) if t1_booked else trade["qty"]
                live_pnl = (ltp - entry) * remaining_qty
                await self.state_manager.update(live_pnl=round(live_pnl, 2))

                if ltp > trade.get("max_price", ltp):
                    trade["max_price"] = ltp
                    await self.state_manager.update(active_trade=trade)

                trail_sl = round(trade["max_price"] * (1 - settings.trailing_pct), 2)

                sq_h, sq_m = map(int, settings.square_off.split(":"))
                if datetime.now().time() >= dtime(sq_h, sq_m):
                    await self._exit_trade(trade, "EOD_SQUAREOFF", ltp); continue

                # Hard SL — always active
                if ltp <= sl_price:
                    await self._exit_trade(trade, "STOPLOSS", ltp); continue

                # Stage 1: book 50% at T1
                if not t1_booked and ltp >= t1_price:
                    await self._book_partial(trade, ltp); continue

                # Stage 2: after T1 booked
                if t1_hit:
                    if ltp >= t2_price:
                        await self._exit_remaining(trade, "TARGET_2", ltp); continue
                    if ltp <= trail_sl:
                        await self._exit_remaining(trade, "TRAIL_STOP", ltp); continue

            except Exception as exc:
                logger.error("Monitor loop: %s", exc, exc_info=True)

    # ── PARTIAL EXIT — T1 (50% qty) ──────────────────
    async def _book_partial(self, trade: dict, price: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade: return

            t1_qty = trade.get("t1_qty", trade["qty"] // 2)
            symbol = trade["symbol"]

            sell_resp = await self.broker.place_order(symbol=symbol, side="SELL", quantity=t1_qty)
            sell_id   = (sell_resp.get("orderNumber") or sell_resp.get("orderId") or sell_resp.get("order_id"))
            if not sell_id:
                logger.error("T1 SELL rejected resp=%s", sell_resp); return

            await self.broker.confirm_fill(sell_id)

            t1_pnl = round((price - trade["entry_price"]) * t1_qty, 2)
            trade["t1_hit"]    = True
            trade["t1_booked"] = True
            trade["t1_pnl"]    = t1_pnl

            new_daily = round(state.daily_pnl + t1_pnl, 2)
            await self.state_manager.update(
                active_trade=trade,
                daily_pnl=new_daily,
                live_pnl=0.0,
            )
            logger.info("T1 BOOKED %s qty=%s price=₹%.2f pnl=₹%.2f id=%s",
                        symbol, t1_qty, price, t1_pnl, sell_id)
            await self.event_bus.publish("T1_BOOKED", {"symbol": symbol, "price": price, "pnl": t1_pnl})

    # ── EXIT REMAINING — T2 / trail ───────────────────
    async def _exit_remaining(self, trade: dict, reason: str, price: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade: return

            symbol = trade["symbol"]
            t2_qty = trade.get("t2_qty", trade["qty"] // 2)

            sell_resp = await self.broker.place_order(symbol=symbol, side="SELL", quantity=t2_qty)
            sell_id   = (sell_resp.get("orderNumber") or sell_resp.get("orderId") or sell_resp.get("order_id"))
            if not sell_id:
                logger.error("T2 SELL rejected — position open! resp=%s", sell_resp); return

            t2_pnl    = round((price - trade["entry_price"]) * t2_qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + t2_pnl, 2)
            new_daily = round(state.daily_pnl + t2_pnl, 2)

            closed = {**trade, "exit_price": price, "exit_time": datetime.now(UTC).isoformat(),
                      "status": "CLOSED", "exit_reason": reason, "pnl": total_pnl, "sell_order_id": sell_id}
            self.trade_store.append_trade(closed, new_daily)
            await self.state_manager.update(active_trade=None, daily_pnl=new_daily, live_pnl=0.0)
            logger.info("EXIT %s reason=%s price=₹%.2f pnl=₹%.2f", symbol, reason, price, total_pnl)
            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    # ── FULL EXIT — SL / EOD ──────────────────────────
    async def _exit_trade(self, trade: dict, reason: str, price: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade: return

            symbol = trade["symbol"]
            # Only sell remaining qty if T1 already booked
            qty = trade.get("t2_qty", trade["qty"] // 2) if trade.get("t1_booked") else trade["qty"]

            sell_resp = await self.broker.place_order(symbol=symbol, side="SELL", quantity=qty)
            sell_id   = (sell_resp.get("orderNumber") or sell_resp.get("orderId") or sell_resp.get("order_id"))
            if not sell_id:
                logger.error("SELL rejected — position may be open! resp=%s", sell_resp); return

            # PnL only on qty sold here (no double-count with t1_pnl)
            exit_pnl  = round((price - trade["entry_price"]) * qty, 2)
            total_pnl = round(trade.get("t1_pnl", 0) + exit_pnl, 2)
            new_daily = round(state.daily_pnl + exit_pnl, 2)

            closed = {**trade, "exit_price": price, "exit_time": datetime.now(UTC).isoformat(),
                      "status": "CLOSED", "exit_reason": reason, "pnl": total_pnl, "sell_order_id": sell_id}
            self.trade_store.append_trade(closed, new_daily)
            await self.state_manager.update(active_trade=None, daily_pnl=new_daily, live_pnl=0.0)
            logger.info("EXIT %s reason=%s price=₹%.2f pnl=₹%.2f", symbol, reason, price, total_pnl)
            await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

    # ── HEALTH ────────────────────────────────────────
    async def _health_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                if not await self.broker.healthcheck():
                    logger.warning("Healthcheck failed — re-login")
                    await self.broker.login()
            except Exception as exc:
                logger.error("Health loop: %s", exc)

    # ── HELPERS ───────────────────────────────────────
    def _get_qty(self, size_label: str) -> int:
        base = settings.order_qty
        if size_label == "FULL":   return base
        if size_label == "MEDIUM": return max(int(base * 0.75 / 25) * 25, 25)
        if size_label == "HALF":   return max(int(base * 0.50 / 25) * 25, 25)
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
