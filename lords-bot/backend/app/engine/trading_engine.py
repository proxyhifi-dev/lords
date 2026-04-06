from __future__ import annotations
import asyncio
from datetime import UTC, datetime

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger
from backend.app.strategy.option_selector import OptionSelector


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

        # OPTION PREMIUM BASED RISK
        self.max_trades_per_day = 3
        self.stoploss_points = 15
        self.target_points = 30


    async def run(self):

        await asyncio.gather(
            self._signal_watcher(),
            self._monitor_trade_loop(),
            self._health_monitor_loop(),
        )


    # ------------------------------------------------
    # SIGNAL WATCHER
    # ------------------------------------------------

    async def _signal_watcher(self):

        while True:

            await asyncio.sleep(1)

            async with self._trade_lock:

                state = await self.state_manager.snapshot()
                signal = state.signal

                if not signal:
                    continue

                if state.active_trade:
                    continue

                if state.trade_count >= self.max_trades_per_day:
                    continue

                try:

                    symbol, strike = OptionSelector.select(
                        state.spot_price,
                        signal
                    )

                    qty = 50

                    # get OPTION price
                    quote = await self.broker.get_quote(
                        symbol_name=symbol,
                        exchange="NFO"
                    )

                    ltp = None

                    if isinstance(quote, dict):

                        details = quote.get("tradeDetails")

                        if details and isinstance(details, list):

                            ltp = float(details[0].get("lastTradedPrice", 0))

                    if ltp is None:
                        continue

                    await self.broker.place_order(
                        symbol=symbol,
                        side="BUY",
                        quantity=qty
                    )

                    trade = {
                        "symbol": symbol,
                        "qty": qty,
                        "entry_price": ltp,
                        "entry_time": datetime.now(UTC).isoformat(),
                        "status": "OPEN",
                        "signal": signal
                    }

                    await self.state_manager.update(
                        active_trade=trade,
                        trade_count=state.trade_count + 1,
                        signal=None
                    )

                    self.logger.info(f"Trade opened {symbol} at {ltp}")

                except Exception as exc:

                    self.logger.error(
                        f"order_execution_failed={exc}"
                    )


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
                    exchange="NFO"
                )

                ltp = None

                if isinstance(quote, dict):

                    details = quote.get("tradeDetails")

                    if details and isinstance(details, list):

                        ltp = float(details[0].get("lastTradedPrice", 0))

                if ltp is None:
                    continue

                entry = trade["entry_price"]
                qty = trade["qty"]

                # LIVE PNL
                live_pnl = (ltp - entry) * qty

                await self.state_manager.update(
                    live_pnl=live_pnl
                )

                # STOPLOSS
                if ltp <= entry - self.stoploss_points:
                    await self._exit_trade("STOPLOSS", ltp)

                # TARGET
                elif ltp >= entry + self.target_points:
                    await self._exit_trade("TARGET", ltp)

            except Exception as exc:

                self.logger.error(
                    f"monitor_error={exc}"
                )


    # ------------------------------------------------
    # EXIT TRADE
    # ------------------------------------------------

    async def _exit_trade(self, reason, exit_price):

        state = await self.state_manager.snapshot()
        trade = state.active_trade

        if not trade:
            return

        symbol = trade["symbol"]
        qty = trade["qty"]

        await self.broker.place_order(
            symbol=symbol,
            side="SELL",
            quantity=qty
        )

        pnl = (exit_price - trade["entry_price"]) * qty

        closed_trade = {
            **trade,
            "exit_price": exit_price,
            "exit_time": datetime.now(UTC).isoformat(),
            "status": "CLOSED",
            "exit_reason": reason,
            "pnl": pnl
        }

        self.trade_store.append_trade(closed_trade)

        await self.state_manager.update(
            active_trade=None,
            daily_pnl=state.daily_pnl + pnl,
            live_pnl=0
        )

        self.logger.info(
            f"Trade closed reason={reason} pnl={pnl}"
        )


    # ------------------------------------------------
    # BROKER HEALTH CHECK
    # ------------------------------------------------

    async def _health_monitor_loop(self):

        while True:

            await asyncio.sleep(20)

            healthy = await self.broker.healthcheck()

            if not healthy:

                self.logger.warning(
                    "Broker unhealthy"
                )