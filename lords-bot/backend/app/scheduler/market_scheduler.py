from __future__ import annotations
import asyncio
from datetime import datetime
from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import state_manager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger

MIN_ORB_RANGE = 5.0
BREAKOUT_BUFFER = 2.0
SIGNAL_COOLDOWN = 10

# TESTING FRIENDLY ORB WINDOW
ORB_DURATION = 120   # seconds


class MarketScheduler:

    def __init__(self):

        self.logger = get_logger("market_scheduler")

        self.state = state_manager
        self.trade_store = TradeStore()
        self.broker = SamcoClient()
        self.event_bus = EventBus()

        self.engine = TradingEngine(
            event_bus=self.event_bus,
            state_manager=self.state,
            trade_store=self.trade_store,
            broker=self.broker,
        )

        self.running = False

        self._task: asyncio.Task | None = None
        self._engine_task: asyncio.Task | None = None

        self._last_signal_time = 0
        self._previous_spot: float | None = None

        # ORB start tracker
        self._orb_start_time = None


    async def start(self):

        if self.running:
            return

        self.logger.info("Starting market scheduler")

        self.running = True

        await self.event_bus.start()
        await self.broker.login()

        self._engine_task = asyncio.create_task(self.engine.run())

        self._task = asyncio.create_task(self._loop())


    async def stop(self):

        if not self.running:
            return

        self.logger.info("Stopping market scheduler")

        self.running = False

        await self.event_bus.stop()

        for task in (self._task, self._engine_task):

            if task and not task.done():
                task.cancel()


    async def _loop(self):

        while self.running:

            try:
                await self._tick()

            except Exception as exc:
                self.logger.error("Market loop error: %s", exc)

            await asyncio.sleep(1)


    async def _resolve_option_symbol(self, signal: str, spot: float):

        try:

            strike = round(spot / 50) * 50
            option_type = "CE" if signal == "CALL" else "PE"

            expiry = datetime.now().strftime("%d-%b-%Y").upper()

            chain = await self.broker.get_option_chain(
                search_symbol_name="NIFTY",
                exchange="NFO",
                expiry_date=expiry,
                strike_price=str(strike),
                option_type=option_type,
            )

            details = chain.get("optionChainDetails")

            if not details:
                return None

            symbol = details[0].get("tradingSymbol")

            self.logger.info("Resolved option symbol: %s", symbol)

            return symbol

        except Exception as e:

            self.logger.error("Option symbol resolve failed: %s", e)

            return None


    async def _validate_and_place_order(self, signal: str, spot: float):

        symbol = await self._resolve_option_symbol(signal, spot)

        if not symbol:
            self.logger.warning("Option symbol not found")
            return

        quote = await self.broker.get_quote(symbol, "NFO")

        ltp = SamcoClient.parse_ltp(quote)

        if not ltp:
            self.logger.warning("Option price unavailable %s", symbol)
            return

        self.logger.info("Option LTP %s = %.2f", symbol, ltp)

        qty = 50

        result = await self.broker.place_order(
            symbol=symbol,
            side="BUY",
            quantity=qty,
        )

        self.logger.info("Order result %s", result)

        await self.state.update(
            active_trade=symbol,
            signal=signal
        )


    async def _tick(self):

        quote = await self.broker.get_index_quote("NIFTY 50")

        spot = SamcoClient.parse_spot(quote)

        if spot is None:

            self.logger.warning("Spot price unavailable")
            return

        self.logger.info("NIFTY spot=%.2f", spot)

        await self.state.update(spot_price=spot)

        await self.event_bus.publish("TICK", {"price": spot})

        state = await self.state.snapshot()

        now = datetime.now().timestamp()

        # -----------------------------------------
        # START ORB TIMER
        # -----------------------------------------

        if self._orb_start_time is None:
            self._orb_start_time = now

        orb_elapsed = now - self._orb_start_time

        # -----------------------------------------
        # ORB BUILD WINDOW
        # -----------------------------------------

        if orb_elapsed < ORB_DURATION:

            high = state.orb_high
            low = state.orb_low

            high = spot if high is None else max(high, spot)
            low = spot if low is None else min(low, spot)

            await self.state.update(
                orb_high=high,
                orb_low=low
            )

            self.logger.info(
                "ORB building high=%.2f low=%.2f",
                high,
                low
            )

            return

        # -----------------------------------------
        # TRADE LOGIC
        # -----------------------------------------

        if state.active_trade:
            return

        if state.orb_high is None or state.orb_low is None:
            return

        orb_range = state.orb_high - state.orb_low

        if orb_range < MIN_ORB_RANGE:
            return

        now_ts = datetime.now().timestamp()

        if now_ts - self._last_signal_time < SIGNAL_COOLDOWN:
            return

        breakout_up = state.orb_high + BREAKOUT_BUFFER
        breakout_down = state.orb_low - BREAKOUT_BUFFER

        if self._previous_spot is None:
            self._previous_spot = spot
            return

        if self._previous_spot <= breakout_up and spot > breakout_up:

            self.logger.info("ORB BREAKOUT UP → CALL %.2f", spot)

            await self.state.update(signal="CALL")

            await self._validate_and_place_order("CALL", spot)

            self._last_signal_time = now_ts

        elif self._previous_spot >= breakout_down and spot < breakout_down:

            self.logger.info("ORB BREAKOUT DOWN → PUT %.2f", spot)

            await self.state.update(signal="PUT")

            await self._validate_and_place_order("PUT", spot)

            self._last_signal_time = now_ts

        self._previous_spot = spot


scheduler = MarketScheduler()