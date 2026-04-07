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
ORB_DURATION = 120
GAP_THRESHOLD = 5


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

        self._orb_start_time = None
        self._orb_logged = False

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

        # ------------------------------------------------
        # ORB START TIMER
        # ------------------------------------------------

        if self._orb_start_time is None:
            self._orb_start_time = now

        orb_elapsed = now - self._orb_start_time

        # ------------------------------------------------
        # BUILD ORB RANGE
        # ------------------------------------------------

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

        # ------------------------------------------------
        # ORB COMPLETE LOG (only once)
        # ------------------------------------------------

        if not self._orb_logged and state.orb_high and state.orb_low:

            self.logger.info(
                "ORB COMPLETE high=%.2f low=%.2f",
                state.orb_high,
                state.orb_low
            )

            self._orb_logged = True

        # ------------------------------------------------
        # TRADE FILTERS
        # ------------------------------------------------

        if state.active_trade:
            return

        if state.signal is not None:
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

        # ------------------------------------------------
        # ULTRA PRO BREAKOUT LOGIC
        # ------------------------------------------------

        call_cross = self._previous_spot <= breakout_up and spot > breakout_up
        put_cross = self._previous_spot >= breakout_down and spot < breakout_down

        call_gap = spot > breakout_up + GAP_THRESHOLD
        put_gap = spot < breakout_down - GAP_THRESHOLD

        if call_cross or call_gap:

            self.logger.info("ORB BREAKOUT UP → CALL %.2f", spot)

            await self.state.update(signal="CALL")

            await self.event_bus.publish(
                "SIGNAL",
                {
                    "signal": "CALL",
                    "spot_price": spot
                }
            )

            self._last_signal_time = now_ts

        elif put_cross or put_gap:

            self.logger.info("ORB BREAKOUT DOWN → PUT %.2f", spot)

            await self.state.update(signal="PUT")

            await self.event_bus.publish(
                "SIGNAL",
                {
                    "signal": "PUT",
                    "spot_price": spot
                }
            )

            self._last_signal_time = now_ts

        self._previous_spot = spot


scheduler = MarketScheduler()