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


class TradingEngine:

    def __init__(
        self,
        event_bus:     EventBus,
        state_manager,
        trade_store:   TradeStore,
        broker:        SamcoClient,
    ):
        self.event_bus     = event_bus
        self.state_manager = state_manager
        self.trade_store   = trade_store
        self.broker        = broker
        self.logger        = get_logger("trading_engine")
        self._trade_lock   = asyncio.Lock()
        self._broker_healthy = True
        self._symbol_cache: dict[str, str] = {}

    # --------------------------------------------------
    # MAIN RUN
    # --------------------------------------------------
    async def run(self):
        await asyncio.gather(
            self._signal_listener(),
            self._trade_executor(),
            self._monitor_trade_loop(),
            self._health_monitor_loop(),
        )

    # --------------------------------------------------
    # SIGNAL LISTENER
    # --------------------------------------------------
    async def _signal_listener(self):
        queue = self.event_bus.subscribe("SIGNAL")
        async for event in self.event_bus.iter_events(queue):
            payload = getattr(event, "payload", event)
            signal  = payload.get("signal")
            if not signal:
                continue
            state = await self.state_manager.snapshot()
            if state.signal is not None or state.active_trade:
                continue
            size_label  = payload.get("size_label",  "FULL")
            trend_score = payload.get("trend_score", 0)
            await self.state_manager.update(
                signal=signal,
                signal_meta={"size_label": size_label, "trend_score": trend_score},
            )
            self.logger.info(
                "Signal received → %s  size=%s  trend=%+d",
                signal, size_label, trend_score,
            )

    # --------------------------------------------------
    # RESOLVE OPTION SYMBOL  (yyyy-mm-dd expiry format)
    # --------------------------------------------------
    async def _resolve_option_symbol(self, strike: int, signal: str) -> str | None:
        cache_key = f"{strike}_{signal}"
        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]

        option_type = OptionSelector.get_option_type(signal)
        expiry      = OptionSelector.get_expiry_api()    # FIXED: yyyy-mm-dd

        self.logger.info(
            "Resolving option: strike=%s signal=%s expiry=%s type=%s",
            strike, signal, expiry, option_type,
        )

        chain = await self.broker.get_option_chain(
            search_symbol_name="NIFTY",
            exchange="NFO",
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=option_type,
        )

        rows = chain.get("optionChainDetails") or chain.get("data") or []
        if not rows:
            self.logger.error(
                "Option chain empty — expiry=%s strike=%s type=%s | chain=%s",
                expiry, strike, option_type, chain,
            )
            return None

        best_symbol = None
        best_diff   = 999_999.0
        for row in rows:
            diff = abs(float(row.get("strikePrice", 0)) - strike)
            if diff < best_diff:
                best_diff   = diff
                best_symbol = row.get("tradingSymbol")

        if not best_symbol:
            self.logger.error("Symbol resolve failed strike=%s signal=%s", strike, signal)
            return None

        self._symbol_cache[cache_key] = best_symbol
        self.logger.info("Resolved: %s", best_symbol)
        return best_symbol

    # --------------------------------------------------
    # TIME-WEIGHTED QTY
    # FULL=50 | MEDIUM=37 | HALF=25
    # --------------------------------------------------
    def _get_qty(self, size_label: str) -> int:
        base = settings.order_qty
        if size_label == "FULL":   return base
        if size_label == "MEDIUM": return max(int(base * 0.75 / 25) * 25, 25)
        if size_label == "HALF":   return max(int(base * 0.50 / 25) * 25, 25)
        return base

    # --------------------------------------------------
    # TRADE EXECUTOR
    # --------------------------------------------------
    async def _trade_executor(self):
        while True:
            await asyncio.sleep(1)

            async with self._trade_lock:
                state = await self.state_manager.snapshot()

                if state.signal is None:           continue
                if state.active_trade:             continue
                if state.trade_count >= settings.max_trades:
                    await self.state_manager.update(signal=None)
                    self.logger.warning("Max trades reached — signal cleared")
                    continue
                if state.daily_pnl <= -abs(settings.max_daily_loss):
                    await self.state_manager.update(signal=None, trading_enabled=False)
                    self.logger.warning("Max daily loss hit — trading disabled")
                    continue
                if not state.trading_enabled:
                    await self.state_manager.update(signal=None)
                    continue
                if state.spot_price is None:       continue

                no_entry = dtime(*map(int, settings.no_entry_after.split(":")))
                if datetime.now().time() >= no_entry:
                    await self.state_manager.update(signal=None)
                    self.logger.info("Past no-entry time — signal cleared")
                    continue

                signal     = state.signal
                meta       = getattr(state, "signal_meta", {}) or {}
                size_label = meta.get("size_label", "FULL")

                try:
                    strike = OptionSelector.get_otm_strike(
                        state.spot_price, signal,
                        distance=settings.otm_distance,
                    )
                    symbol = await self._resolve_option_symbol(strike, signal)
                    if not symbol:
                        await self.state_manager.update(signal=None); continue

                    quote = await self.broker.get_quote(symbol_name=symbol, exchange="NFO")
                    ltp   = self.broker.parse_ltp(quote)
                    if not ltp:
                        self.logger.warning("LTP unavailable for %s", symbol)
                        await self.state_manager.update(signal=None); continue

                    # Min premium filter
                    if ltp < settings.min_entry_premium:
                        self.logger.warning(
                            "Premium ₹%.1f below min ₹%.1f — skipping %s",
                            ltp, settings.min_entry_premium, symbol,
                        )
                        await self.state_manager.update(signal=None); continue

                    # Volume filter
                    volume = quote.get("volume") or quote.get("tradedVolume") or 0
                    try:    volume = int(volume)
                    except: volume = 0
                    if volume < settings.min_option_volume:
                        self.logger.warning("Low volume %s for %s", volume, symbol)
                        await self.state_manager.update(signal=None); continue

                    qty = self._get_qty(size_label)

                    # Place BUY order — capture order ID for fill confirmation
                    order_resp = await self.broker.place_order(
                        symbol=symbol, side="BUY", quantity=qty,
                    )
                    order_id = (
                        order_resp.get("orderNumber") or
                        order_resp.get("orderId")     or
                        order_resp.get("order_id")
                    )
                    if not order_id:
                        self.logger.error("BUY rejected — no order ID — resp=%s", order_resp)
                        await self.state_manager.update(signal=None); continue

                    self.logger.info(
                        "BUY placed — order_id=%s  symbol=%s  ltp=%.2f  qty=%s  size=%s",
                        order_id, symbol, ltp, qty, size_label,
                    )

                    # 2-stage exit levels
                    t1_qty = qty // 2
                    t2_qty = qty - t1_qty

                    trade = {
                        "symbol":      symbol,
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
                    }

                    await self.state_manager.update(
                        active_trade=trade,
                        trade_count=state.trade_count + 1,
                        signal=None,
                        signal_meta=None,
                    )
                    self.logger.info(
                        "Trade OPENED — %s  entry=%.2f  qty=%s  SL=%.2f  T1=%.2f  T2=%.2f",
                        symbol, ltp, qty,
                        trade["sl_price"], trade["t1_price"], trade["t2_price"],
                    )

                except Exception as exc:
                    self.logger.error("Trade execution failed: %s", exc, exc_info=True)
                    await self.state_manager.update(signal=None)

    # --------------------------------------------------
    # MONITOR TRADE LOOP — 2-STAGE EXIT
    # Stage 1: book 50% qty at T1 (40% profit)
    # Stage 2: trail remaining 50% with 25% trail (only after T1)
    #          or exit at T2 (100% profit)
    # Hard SL: always active on full remaining position
    # --------------------------------------------------
    async def _monitor_trade_loop(self):
        while True:
            await asyncio.sleep(2)

            state = await self.state_manager.snapshot()
            trade = state.active_trade
            if not trade:
                continue

            try:
                quote = await self.broker.get_quote(
                    symbol_name=trade["symbol"], exchange="NFO"
                )
                ltp = self.broker.parse_ltp(quote)
                if not ltp:
                    continue

                entry     = trade["entry_price"]
                qty       = trade["qty"]
                pnl       = (ltp - entry) * qty
                await self.state_manager.update(live_pnl=pnl)

                trade["max_price"] = max(trade.get("max_price", ltp), ltp)
                trail_sl  = round(trade["max_price"] * (1 - settings.trailing_pct), 2)
                sl_price  = trade.get("sl_price",  round(entry * (1 - settings.stop_loss_pct), 2))
                t1_price  = trade.get("t1_price",  round(entry * (1 + settings.t1_pct), 2))
                t2_price  = trade.get("t2_price",  round(entry * (1 + settings.t2_pct), 2))
                t1_hit    = trade.get("t1_hit",    False)
                t1_booked = trade.get("t1_booked", False)
                sq_time   = dtime(*map(int, settings.square_off.split(":")))
                now_time  = datetime.now().time()

                # EOD square off
                if now_time >= sq_time:
                    await self._exit_trade(trade, "EOD", ltp); continue

                # Hard SL — always active
                if ltp <= sl_price:
                    await self._exit_trade(trade, "STOPLOSS", ltp); continue

                # Stage 1: partial profit booking at T1
                if not t1_booked and ltp >= t1_price:
                    trade["t1_hit"]    = True
                    trade["t1_booked"] = True
                    await self._book_partial(trade, ltp)
                    await self.state_manager.update(active_trade=trade)
                    self.logger.info(
                        "T1 HIT ₹%.2f — booked %s lots, monitoring remaining %s lots",
                        ltp, trade["t1_qty"], trade["t2_qty"],
                    )
                    continue

                # Stage 2: after T1 booked
                if t1_hit:
                    if ltp >= t2_price:
                        await self._exit_remaining(trade, "TARGET_2", ltp); continue
                    if ltp < trail_sl:
                        await self._exit_remaining(trade, "TRAIL_AFTER_T1", ltp); continue

            except Exception as exc:
                self.logger.error("Monitor loop error: %s", exc, exc_info=True)

    # --------------------------------------------------
    # BOOK PARTIAL — sell T1 qty (50% of position)
    # --------------------------------------------------
    async def _book_partial(self, trade: dict, price: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                return

            t1_qty = trade.get("t1_qty", trade["qty"] // 2)
            symbol = trade["symbol"]

            try:
                sell_resp = await self.broker.place_order(
                    symbol=symbol, side="SELL", quantity=t1_qty,
                )
                sell_id = (
                    sell_resp.get("orderNumber") or
                    sell_resp.get("orderId")     or
                    sell_resp.get("order_id")
                )
                if not sell_id:
                    self.logger.error("T1 partial SELL failed — resp=%s", sell_resp)
                    return

                t1_pnl = round((price - trade["entry_price"]) * t1_qty, 2)
                self.logger.info(
                    "T1 BOOKED — symbol=%s  qty=%s  price=%.2f  pnl=₹%.2f  id=%s",
                    symbol, t1_qty, price, t1_pnl, sell_id,
                )
                await self.state_manager.update(
                    daily_pnl=round(state.daily_pnl + t1_pnl, 2),
                    live_pnl=0.0,
                )

            except Exception as exc:
                self.logger.error("T1 partial sell error: %s", exc)

    # --------------------------------------------------
    # EXIT REMAINING — sell T2 qty (remaining 50%)
    # --------------------------------------------------
    async def _exit_remaining(self, trade: dict, reason: str, price: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                return

            symbol = trade["symbol"]
            t2_qty = trade.get("t2_qty", trade["qty"] // 2)

            try:
                sell_resp = await self.broker.place_order(
                    symbol=symbol, side="SELL", quantity=t2_qty,
                )
                sell_id = (
                    sell_resp.get("orderNumber") or
                    sell_resp.get("orderId")     or
                    sell_resp.get("order_id")
                )
                if not sell_id:
                    self.logger.error("T2 SELL failed — position still open! resp=%s", sell_resp)
                    return
            except Exception as exc:
                self.logger.error("T2 sell error: %s — position still open!", exc)
                return

            t2_pnl = round((price - trade["entry_price"]) * t2_qty, 2)
            closed = {
                **trade,
                "exit_price":    price,
                "exit_time":     datetime.now(UTC).isoformat(),
                "status":        "CLOSED",
                "exit_reason":   reason,
                "pnl":           t2_pnl,
                "sell_order_id": sell_id,
            }
            self.trade_store.append_trade(closed)
            await self.state_manager.update(
                active_trade=None,
                daily_pnl=round(state.daily_pnl + t2_pnl, 2),
                live_pnl=0.0,
            )
            self.logger.info(
                "STAGE 2 CLOSED — %s  reason=%s  price=%.2f  pnl=₹%.2f  id=%s",
                symbol, reason, price, t2_pnl, sell_id,
            )

    # --------------------------------------------------
    # EXIT TRADE — full position exit (SL / EOD)
    # If T1 already booked, only exits remaining T2 qty
    # --------------------------------------------------
    async def _exit_trade(self, trade: dict, reason: str, price: float):
        async with self._trade_lock:
            state = await self.state_manager.snapshot()
            if not state.active_trade:
                return

            symbol = trade["symbol"]
            # If T1 already booked → only sell remaining t2_qty
            qty = trade.get("t2_qty", trade["qty"] // 2) if trade.get("t1_booked") else trade["qty"]

            try:
                sell_resp = await self.broker.place_order(
                    symbol=symbol, side="SELL", quantity=qty,
                )
                sell_id = (
                    sell_resp.get("orderNumber") or
                    sell_resp.get("orderId")     or
                    sell_resp.get("order_id")
                )
                if not sell_id:
                    self.logger.error("SELL rejected — position may still be open! resp=%s", sell_resp)
                    return
            except Exception as exc:
                self.logger.error("SELL error: %s — position may still be open!", exc)
                return

            pnl = round((price - trade["entry_price"]) * qty, 2)
            closed = {
                **trade,
                "exit_price":    price,
                "exit_time":     datetime.now(UTC).isoformat(),
                "status":        "CLOSED",
                "exit_reason":   reason,
                "pnl":           pnl,
                "sell_order_id": sell_id,
            }
            self.trade_store.append_trade(closed)
            await self.state_manager.update(
                active_trade=None,
                daily_pnl=round(state.daily_pnl + pnl, 2),
                live_pnl=0.0,
            )
            self.logger.info(
                "Trade CLOSED — %s  reason=%s  price=%.2f  pnl=₹%.2f  id=%s",
                symbol, reason, price, pnl, sell_id,
            )

    # --------------------------------------------------
    # BROKER HEALTH MONITOR
    # --------------------------------------------------
    async def _health_monitor_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                healthy = await self.broker.healthcheck()
                self._broker_healthy = healthy
                if not healthy:
                    self.logger.warning("Broker healthcheck FAILED — re-login")
                    await self.broker.login()
            except Exception as exc:
                self.logger.error("Health monitor error: %s", exc)