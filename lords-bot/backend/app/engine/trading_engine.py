from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger


NO_ENTRY_AFTER = time(14, 0)
SQUAREOFF_TIME = time(15, 15)
MAX_DAILY_LOSS = -5000.0


class TradingEngine:

    def __init__(
        self,
        event_bus: EventBus,
        state_manager: StateManager,
        trade_store: TradeStore,
        broker: SamcoClient,
    ):

        self.event_bus = event_bus
        self.state_manager = state_manager
        self.trade_store = trade_store
        self.broker = broker

        self.logger = get_logger("trading_engine")

        self._trade_lock = asyncio.Lock()
        self._broker_healthy = True

        self.max_trades_per_day = 3
        self.stoploss_pct = 0.30
        self.target_pct = 0.60

    async def run(self):

        await asyncio.gather(
            self._signal_listener(),
            self._signal_watcher(),
            self._monitor_trade_loop(),
            self._health_monitor_loop(),
        )

    # ------------------------------------------------
    # SIGNAL LISTENER
    # ------------------------------------------------

    async def _signal_listener(self):

        queue = self.event_bus.subscribe("SIGNAL")

        async for event in self.event_bus.iter_events(queue):

            try:

                payload = getattr(event, "payload", event)

                signal = payload.get("signal")

                if not signal:
                    continue

                # 🔥 FIX
                await self.state_manager.update(signal=signal)

                self.logger.info("Signal event received → %s", signal)

            except Exception as exc:

                self.logger.error("Signal parse error: %s", exc)

    # ------------------------------------------------
    # OPTION SYMBOL RESOLVER
    # ------------------------------------------------

    async def _resolve_option_symbol(self, strike: int, signal: str):

        option_type = OptionSelector.get_option_type(signal)

        today = datetime.now()

        days = 3 - today.weekday()

        if days <= 0:
            days += 7

        expiry = today + timedelta(days=days)

        expiry_date = expiry.strftime("%Y-%m-%d")

        try:

            chain = await self.broker.get_option_chain(
                search_symbol_name="NIFTY",
                exchange="NFO",
                expiry_date=expiry_date,
                strike_price=str(strike),
                option_type=option_type,
            )

            rows = (
                chain.get("optionChainDetails")
                or chain.get("data")
                or []
            )

            if not rows:

                self.logger.warning("Empty option chain response")

                return None

            symbol = rows[0].get("tradingSymbol")

            return symbol

        except Exception as exc:

            self.logger.error("option_symbol_resolve_failed: %s", exc)

            return None

    # ------------------------------------------------
    # SIGNAL WATCHER
    # ------------------------------------------------

    async def _signal_watcher(self):

        while True:

            await asyncio.sleep(1)

            async with self._trade_lock:

                state = await self.state_manager.snapshot()

                signal = state.signal

                if signal is None:
                    continue

                if state.active_trade:
                    continue

                now = datetime.now().time()

                if now >= NO_ENTRY_AFTER:

                    await self.state_manager.update(signal=None)
                    continue

                if state.trade_count >= self.max_trades_per_day:

                    await self.state_manager.update(signal=None)
                    continue

                if state.daily_pnl <= MAX_DAILY_LOSS:

                    await self.state_manager.update(signal=None)
                    continue

                if not self._broker_healthy:
                    continue

                if state.spot_price is None:
                    continue

                try:

                    strike = OptionSelector.get_atm_strike(
                        state.spot_price
                    )

                    symbol = await self._resolve_option_symbol(
                        strike,
                        signal,
                    )

                    if not symbol:

                        self.logger.warning("Option symbol not resolved")
                        continue

                    self.logger.info("Resolved option symbol → %s", symbol)

                    quote = await self.broker.get_quote(
                        symbol_name=symbol,
                        exchange="NFO",
                    )

                    ltp = self.broker.parse_ltp(quote)

                    if ltp is None:

                        self.logger.warning("LTP unavailable %s", symbol)
                        continue

                    qty = 50

                    self.logger.info(
                        "Placing BUY order %s qty=%s",
                        symbol,
                        qty,
                    )

                    await self.broker.place_order(
                        symbol=symbol,
                        side="BUY",
                        quantity=qty,
                    )

                    trade = {
                        "symbol": symbol,
                        "qty": qty,
                        "entry_price": ltp,
                        "entry_time": datetime.now(UTC).isoformat(),
                        "status": "OPEN",
                        "signal": signal,
                    }

                    await self.state_manager.update(
                        active_trade=trade,
                        trade_count=state.trade_count + 1,
                        signal=None,
                    )

                    self.logger.info(
                        "Trade opened %s entry=%.2f",
                        symbol,
                        ltp,
                    )

                except Exception as exc:

                    self.logger.error("order_execution_failed: %s", exc)

    # ------------------------------------------------
    # TRADE MONITOR
    # ------------------------------------------------

    async def _monitor_trade_loop(self):

        while True:

            await asyncio.sleep(2)

            state = await self.state_manager.snapshot()

            trade = state.active_trade

            if not trade:
                continue

            try:

                quote = await self.broker.get_quote(
                    symbol_name=trade["symbol"],
                    exchange="NFO",
                )

                ltp = self.broker.parse_ltp(quote)

                if ltp is None:
                    continue

                entry = trade["entry_price"]

                qty = trade["qty"]

                live_pnl = (ltp - entry) * qty

                await self.state_manager.update(live_pnl=live_pnl)

                now = datetime.now().time()

                if now >= SQUAREOFF_TIME:

                    await self._exit_trade("EOD_SQUAREOFF", ltp)

                elif ltp <= entry * (1 - self.stoploss_pct):

                    await self._exit_trade("STOPLOSS", ltp)

                elif ltp >= entry * (1 + self.target_pct):

                    await self._exit_trade("TARGET", ltp)

            except Exception as exc:

                self.logger.error("monitor_error: %s", exc)

    # ------------------------------------------------
    # EXIT TRADE
    # ------------------------------------------------

    async def _exit_trade(self, reason: str, exit_price: float):

        async with self._trade_lock:

            state = await self.state_manager.snapshot()

            trade = state.active_trade

            if not trade:
                return

            symbol = trade["symbol"]
            qty = trade["qty"]

            try:

                await self.broker.place_order(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                )

            except Exception as exc:

                self.logger.error("SELL order failed %s", exc)

            pnl = (exit_price - trade["entry_price"]) * qty

            closed_trade = {
                **trade,
                "exit_price": exit_price,
                "exit_time": datetime.now(UTC).isoformat(),
                "status": "CLOSED",
                "exit_reason": reason,
                "pnl": pnl,
            }

            self.trade_store.append_trade(closed_trade)

            await self.state_manager.update(
                active_trade=None,
                daily_pnl=state.daily_pnl + pnl,
                live_pnl=0,
            )

            self.logger.info(
                "Trade closed %s pnl=%.2f",
                symbol,
                pnl,
            )

    # ------------------------------------------------
    # HEALTH MONITOR
    # ------------------------------------------------

    async def _health_monitor_loop(self):

        while True:

            await asyncio.sleep(20)

            healthy = await self.broker.healthcheck()

            if healthy:

                if not self._broker_healthy:

                    self.logger.info("Broker recovered")

                    self._broker_healthy = True

            else:

                if self._broker_healthy:

                    self.logger.warning("Broker unhealthy")

                    self._broker_healthy = False