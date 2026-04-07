from __future__ import annotations
import asyncio
from datetime import UTC, datetime, time
from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.storage.trade_store import TradeStore
from backend.app.strategy.option_selector import OptionSelector
from backend.app.utils.logger import get_logger

NO_ENTRY_AFTER = time(14, 0)
SQUAREOFF_TIME = time(15, 15)
MAX_DAILY_LOSS = -5000.0

MIN_OPTION_VOLUME = 100
TRADE_COOLDOWN = 10


class TradingEngine:

    def __init__(
        self,
        event_bus: EventBus,
        state_manager,
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
        self._symbol_cache = {}

        self._last_trade_time = 0

        self.max_trades_per_day = 3

        self.stoploss_pct = 0.30
        self.target_pct = 0.60

        self.trailing_pct = 0.20

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

                state = await self.state_manager.snapshot()

                if state.signal is not None:
                    continue

                await self.state_manager.update(signal=signal)

                self.logger.info("Signal event received → %s", signal)

            except Exception as exc:

                self.logger.error("Signal parse error: %s", exc)

    # ------------------------------------------------
    # OPTION SYMBOL
    # ------------------------------------------------

    async def _resolve_option_symbol(self, strike: int, signal: str):

        cache_key = f"{strike}_{signal}"

        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]

        option_type = OptionSelector.get_option_type(signal)

        expiry_date = OptionSelector.get_next_thursday_iso()

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

                self.logger.warning("Empty option chain")

                return None

            symbol = rows[0].get("tradingSymbol")

            self._symbol_cache[cache_key] = symbol

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

                if not self._broker_healthy:
                    self.logger.warning("Broker unhealthy — skip trade")
                    await self.state_manager.update(signal=None)
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

                if state.spot_price is None:
                    continue

                try:

                    strike = OptionSelector.get_atm_strike(state.spot_price)

                    symbol = await self._resolve_option_symbol(
                        strike,
                        signal,
                    )

                    if not symbol:
                        await self.state_manager.update(signal=None)
                        continue

                    quote = await self.broker.get_quote(
                        symbol_name=symbol,
                        exchange="NFO",
                    )

                    ltp = self.broker.parse_ltp(quote)

                    volume = quote.get("volume", 0)

                    if volume < MIN_OPTION_VOLUME:

                        self.logger.warning(
                            "Liquidity filter blocked trade volume=%s",
                            volume,
                        )

                        await self.state_manager.update(signal=None)

                        continue

                    if ltp is None or ltp <= 0:

                        self.logger.warning(
                            "Invalid option LTP symbol=%s ltp=%s",
                            symbol,
                            ltp,
                        )

                        await self.state_manager.update(signal=None)

                        continue

                    qty = 50

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
                        "max_price": ltp,
                    }

                    await self.state_manager.update(
                        active_trade=trade,
                        trade_count=state.trade_count + 1,
                        signal=None,
                    )

                    self._last_trade_time = datetime.now().timestamp()

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

            if not self._broker_healthy:
                continue

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

                if ltp is None or ltp <= 0:
                    continue

                entry = trade["entry_price"]

                qty = trade["qty"]

                pnl = (ltp - entry) * qty

                await self.state_manager.update(live_pnl=pnl)

                trade["max_price"] = max(trade["max_price"], ltp)

                trailing_stop = trade["max_price"] * (1 - self.trailing_pct)

                now = datetime.now().time()

                if now >= SQUAREOFF_TIME:
                    await self._exit_trade("EOD", ltp)

                elif ltp <= entry * (1 - self.stoploss_pct):
                    await self._exit_trade("STOPLOSS", ltp)

                elif ltp >= entry * (1 + self.target_pct):
                    await self._exit_trade("TARGET", ltp)

                elif ltp < trailing_stop:
                    await self._exit_trade("TRAILING_STOP", ltp)

            except Exception as exc:

                self.logger.error("monitor_error: %s", exc)

    # ------------------------------------------------
    # EXIT TRADE
    # ------------------------------------------------

    async def _exit_trade(self, reason: str, price: float):

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

            pnl = (price - trade["entry_price"]) * qty

            closed_trade = {
                **trade,
                "exit_price": price,
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

            self.logger.info("Trade closed %s pnl=%.2f", symbol, pnl)

    # ------------------------------------------------
    # BROKER HEALTH
    # ------------------------------------------------

    async def _health_monitor_loop(self):

        while True:

            await asyncio.sleep(20)

            healthy = await self.broker.healthcheck()

            self._broker_healthy = healthy