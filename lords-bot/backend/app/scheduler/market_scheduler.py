from __future__ import annotations

import asyncio
from datetime import datetime, time

from backend.app.engine.trading_engine import TradingEngine
from backend.app.engine.state_manager import StateManager
from backend.app.storage.trade_store import TradeStore
from backend.app.broker.samco_client import SamcoClient
from backend.app.utils.logger import get_logger
from backend.app.core.event_bus import EventBus


class MarketScheduler:

    def __init__(self):

        self.logger = get_logger("market_scheduler")

        # Core Components
        self.state = StateManager()
        self.trade_store = TradeStore()
        self.broker = SamcoClient()

        # Event Bus
        self.event_bus = EventBus()

        # Trading Engine
        self.engine = TradingEngine(
            event_bus=self.event_bus,
            state_manager=self.state,
            trade_store=self.trade_store,
            broker=self.broker
        )

        self.running = False
        self._task = None
        self._engine_task = None

    # --------------------------------
    # START
    # --------------------------------
    async def start(self):

        if self.running:
            return

        self.logger.info("Starting market scheduler")

        self.running = True

        # login to broker
        await self.broker.login()

        # start trading engine
        self._engine_task = asyncio.create_task(
            self.engine.run()
        )

        # start scheduler loop
        self._task = asyncio.create_task(
            self._loop()
        )

    # --------------------------------
    # STOP
    # --------------------------------
    async def stop(self):

        if not self.running:
            return

        self.logger.info("Stopping market scheduler")

        self.running = False

        if self._task:
            self._task.cancel()

        if self._engine_task:
            self._engine_task.cancel()

    # --------------------------------
    # MAIN LOOP
    # --------------------------------
    async def _loop(self):

        while self.running:

            now = datetime.now().time()

            market_open = time(9, 15)
            market_close = time(15, 15)

            if market_open <= now <= market_close:

                try:

                    # fetch NIFTY spot
                    quote = await self.broker.get_index_quote(
                        "NIFTY 50"
                    )

                    spot = None

                    if isinstance(quote, dict):

                        details = quote.get("indexDetails")

                        if details and isinstance(details, list):

                            spot = details[0].get("spotPrice")

                    if spot is not None:

                        try:
                            spot = float(spot)
                        except Exception:
                            pass

                        # publish tick event
                        await self.event_bus.publish(
                            "TICK",
                            {
                                "price": spot
                            }
                        )

                except Exception as e:

                    self.logger.error(
                        "NIFTY spot update error: %s",
                        e
                    )

            await asyncio.sleep(1)


# --------------------------------
# GLOBAL INSTANCE
# --------------------------------
scheduler = MarketScheduler()