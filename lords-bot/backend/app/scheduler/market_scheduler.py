from __future__ import annotations

import asyncio
from datetime import datetime, time

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.engine.trading_engine import TradingEngine
from backend.app.storage.trade_store import TradeStore
from backend.app.utils.logger import get_logger


MIN_ORB_RANGE = 5.0
BREAKOUT_BUFFER = 2.0
SIGNAL_COOLDOWN = 10


class MarketScheduler:

    def __init__(self):

        self.logger = get_logger("market_scheduler")

        self.state = StateManager()
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
        self._rebuild_task: asyncio.Task | None = None

        self.orb_built = False
        self._orb_rebuild_in_progress = False
        self._last_reset_date: str | None = None
        self._last_signal_time: float = 0

        self._previous_spot: float | None = None

    # ------------------------------------------------
    # START
    # ------------------------------------------------

    async def start(self):

        if self.running:
            return

        self.logger.info("Starting market scheduler")

        self.running = True

        await self.broker.login()

        self._engine_task = asyncio.create_task(self.engine.run())

        self._task = asyncio.create_task(self._loop())

    # ------------------------------------------------
    # STOP
    # ------------------------------------------------

    async def stop(self):

        if not self.running:
            return

        self.logger.info("Stopping market scheduler")

        self.running = False

        for task in (self._task, self._engine_task, self._rebuild_task):

            if task and not task.done():
                task.cancel()

    # ------------------------------------------------
    # DAILY RESET
    # ------------------------------------------------

    async def _daily_reset_if_needed(self):

        today = datetime.now().strftime("%Y-%m-%d")

        if self._last_reset_date == today:
            return

        self.logger.info("New trading day %s — performing reset", today)

        self.orb_built = False
        self._orb_rebuild_in_progress = False
        self._last_signal_time = 0
        self._previous_spot = None

        await self.state.update(
            orb_high=None,
            orb_low=None,
            trade_count=0,
            daily_pnl=0.0,
            live_pnl=0.0,
            active_trade=None,
            signal=None,
        )

        self._last_reset_date = today

    # ------------------------------------------------
    # ORB REBUILD
    # ------------------------------------------------

    async def rebuild_orb(self):

        self.logger.info("ORB rebuild started")

        high = None
        low = None

        for _ in range(30):

            try:

                quote = await self.broker.get_index_quote("NIFTY 50")

                spot = SamcoClient.parse_spot(quote)

                if spot is not None:

                    high = spot if high is None else max(high, spot)
                    low = spot if low is None else min(low, spot)

            except Exception as exc:

                self.logger.warning("ORB rebuild quote error: %s", exc)

            await asyncio.sleep(2)

        if high is not None and low is not None:

            orb_range = high - low

            await self.state.update(
                orb_high=high,
                orb_low=low,
            )

            await self.event_bus.publish(
                "ORB_UPDATED",
                {
                    "orb_high": high,
                    "orb_low": low,
                    "range": orb_range,
                },
            )

            self.logger.info(
                "ORB rebuilt high=%.2f low=%.2f range=%.2f",
                high,
                low,
                orb_range,
            )

        self.orb_built = True
        self._orb_rebuild_in_progress = False

    # ------------------------------------------------
    # MAIN LOOP
    # ------------------------------------------------

    async def _loop(self):

        while self.running:

            now = datetime.now().time()

            await self._daily_reset_if_needed()

            if time(9, 15) <= now <= time(15, 30):

                try:
                    await self._tick(now)

                except Exception as exc:
                    self.logger.error("Market loop error: %s", exc)

            await asyncio.sleep(2)

    # ------------------------------------------------
    # TICK
    # ------------------------------------------------

    async def _tick(self, now: time):

        quote = await self.broker.get_index_quote("NIFTY 50")

        spot = SamcoClient.parse_spot(quote)

        if spot is None:

            self.logger.warning("Spot price unavailable")

            return

        await self.state.update(spot_price=spot)

        await self.event_bus.publish("TICK", {"price": spot})

        state = await self.state.snapshot()

        # ORB BUILD WINDOW

        if time(9, 15) <= now <= time(9, 30):

            high = state.orb_high
            low = state.orb_low

            high = spot if high is None else max(high, spot)
            low = spot if low is None else min(low, spot)

            await self.state.update(
                orb_high=high,
                orb_low=low,
            )

            self._previous_spot = spot

            return

        # ORB REBUILD

        if now > time(9, 30):

            if (
                state.orb_high is None
                and not self.orb_built
                and not self._orb_rebuild_in_progress
            ):

                self._orb_rebuild_in_progress = True

                self._rebuild_task = asyncio.create_task(self.rebuild_orb())

                return

            if self._orb_rebuild_in_progress:
                return

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

        if self._previous_spot is None:

            self._previous_spot = spot

            return

        # CALL BREAKOUT

        if (
            self._previous_spot <= state.orb_high + BREAKOUT_BUFFER
            and spot > state.orb_high + BREAKOUT_BUFFER
        ):

            self.logger.info("ORB BREAKOUT UP → CALL spot=%.2f", spot)

            await self.event_bus.publish(
                "SIGNAL",
                {
                    "signal": "CALL",
                    "price": spot,
                    "time": datetime.now().isoformat(),
                },
            )

            self._last_signal_time = now_ts

        # PUT BREAKOUT

        elif (
            self._previous_spot >= state.orb_low - BREAKOUT_BUFFER
            and spot < state.orb_low - BREAKOUT_BUFFER
        ):

            self.logger.info("ORB BREAKOUT DOWN → PUT spot=%.2f", spot)

            await self.event_bus.publish(
                "SIGNAL",
                {
                    "signal": "PUT",
                    "price": spot,
                    "time": datetime.now().isoformat(),
                },
            )

            self._last_signal_time = now_ts

        self._previous_spot = spot


scheduler = MarketScheduler()