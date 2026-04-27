# backend/app/engine/trading_engine.py

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
_SELL_RETRY_DELAY = 1.5


class TradingEngine:

    def __init__(self, event_bus: EventBus, state_manager,
                 trade_store: TradeStore, broker: SamcoClient):
        self.event_bus     = event_bus
        self.state_manager = state_manager
        self.trade_store   = trade_store
        self.broker        = broker
        self._symbol_cache: dict[str, str] = {}

    async def run(self):
        await asyncio.gather(
            self._entry_listener(),
            self._monitor_loop(),
            self._health_loop(),
        )

    # ───────────────── ENTRY ─────────────────
    async def _entry_listener(self):
        queue = self.event_bus.subscribe("RISK_APPROVED")
        async for event in self.event_bus.iter_events(queue):
            await self._enter_trade(event.payload)

    async def _enter_trade(self, payload: dict):

        async with self.state_manager.lock:

            state = await self.state_manager.snapshot()

            if state.active_trade or not state.trading_enabled:
                return

            if payload.get("price") is None:
                return

            signal = payload.get("signal")
            qty    = payload.get("qty")

            try:
                strike = OptionSelector.get_otm_strike(
                    state.spot_price, signal, distance=settings.otm_distance)

                symbol = await self._resolve_symbol(strike, signal)
                if not symbol:
                    return

                order_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol,
                    side="BUY",
                    quantity=qty
                )

                if not order_id:
                    await self.state_manager.update(last_order_failed=True)
                    await self.event_bus.publish("ORDER_FAILED", {"side": "BUY"})
                    return

                entry_price = fill_price or payload.get("price")

                await self.state_manager.update(last_order_failed=False)

                trade = {
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "qty": qty,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "status": "OPEN",
                    "exiting": False  # ✅ exit guard
                }

                await self.state_manager.update(
                    active_trade=trade,
                    trade_count=state.trade_count + 1
                )

                logger.info("TRADE OPEN %s qty=%d price=%.2f",
                            symbol, qty, entry_price)

                await self.event_bus.publish("TRADE_OPENED", {"trade": trade})

            except Exception as e:
                logger.error("Entry failed: %s", e, exc_info=True)
                await self.state_manager.update(last_order_failed=True)

    # ───────────────── MONITOR ─────────────────
    async def _monitor_loop(self):

        while True:
            await asyncio.sleep(2)

            async with self.state_manager.lock:  # ✅ FIXED RACE
                state = await self.state_manager.snapshot()
                trade = state.active_trade

            if not trade:
                continue

            try:
                quote = await self.broker.get_quote(
                    symbol_name=trade["symbol"], exchange="NFO")

                ltp = self.broker.parse_ltp(quote)
                if not ltp:
                    continue

                entry = trade["entry_price"]
                qty   = trade["qty"]

                pnl = (ltp - entry) * qty

                await self.state_manager.update(live_pnl=round(pnl, 2))
                await self.state_manager.update_unrealized(ltp)

                # ✅ EXIT GUARD
                if trade.get("exiting"):
                    continue

                if pnl < -settings.max_loss_per_trade:
                    trade["exiting"] = True
                    await self.state_manager.update(active_trade=trade)
                    await self._exit_trade(trade, "STOPLOSS", ltp)

            except Exception as e:
                logger.error("Monitor error: %s", e)

    # ───────────────── EXIT ─────────────────
    async def _exit_trade(self, trade: dict, reason: str, ltp: float):

        async with self.state_manager.lock:

            state = await self.state_manager.snapshot()

            if not state.active_trade:
                return

            try:
                sell_id, fill_price = await self._sell_with_retry(
                    trade["symbol"], trade["qty"], reason)

                if not sell_id:
                    logger.critical("SELL FAILED — disabling trading")
                    await self.state_manager.update(trading_enabled=False)
                    await self.event_bus.publish("SELL_FAILED_CRITICAL", {})
                    return

                exit_price = fill_price or ltp
                pnl = (exit_price - trade["entry_price"]) * trade["qty"]
                new_daily = state.daily_pnl + pnl

                closed = {
                    **trade,
                    "exit_price": exit_price,
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "pnl": pnl,
                    "exit_reason": reason,
                }

                self.trade_store.append_trade(closed, new_daily)

                await self.state_manager.update(
                    active_trade=None,
                    daily_pnl=new_daily,
                    unrealized_pnl=0.0
                )

                if pnl < 0:
                    await self.state_manager.trigger_cooldown(15)

                logger.info("EXIT %s pnl=%.2f", trade["symbol"], pnl)

                await self.event_bus.publish("TRADE_CLOSED", {"trade": closed})

            except Exception as e:
                logger.error("Exit failed: %s", e)

    # ───────────────── SELL RETRY ─────────────────
    async def _sell_with_retry(self, symbol: str, qty: int, reason: str):

        for _ in range(_SELL_MAX_RETRIES):
            try:
                order_id, fill_price = await self.broker.place_order_and_wait_fill(
                    symbol=symbol, side="SELL", quantity=qty
                )
                if order_id:
                    return order_id, fill_price
            except Exception:
                await asyncio.sleep(_SELL_RETRY_DELAY)

        return None, None

    # ───────────────── HEALTH ─────────────────
    async def _health_loop(self):

        while True:
            await asyncio.sleep(60)

            try:
                if not await self.broker.healthcheck():
                    logger.warning("Broker down — re-login")
                    await self.broker.login()
            except Exception as e:
                logger.error("Health error: %s", e)

    # ───────────────── SYMBOL RESOLUTION (FIXED) ─────────────────
    async def _resolve_symbol(self, strike: int, signal: str):

        key = f"{strike}_{signal}"

        if key in self._symbol_cache:
            return self._symbol_cache[key]

        opt_type = OptionSelector.get_option_type(signal)
        expiry   = OptionSelector.get_expiry_api()

        chain = await self.broker.get_option_chain(
            search_symbol_name="NIFTY",
            exchange="NFO",
            expiry_date=expiry,
            strike_price=str(strike),
            option_type=opt_type,
        )

        rows = chain.get("optionChainDetails") or []

        if not rows:
            return None

        # ✅ FIX: pick closest strike
        best = min(rows, key=lambda r: abs(float(r.get("strikePrice", 0)) - strike))
        symbol = best.get("tradingSymbol")

        self._symbol_cache[key] = symbol
        return symbol