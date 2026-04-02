from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from config import settings
from services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)


@dataclass
class TickEvent:
    symbol: str
    price: float
    timestamp: str


class MarketFeedEngine:
    def __init__(self, market_data_service: MarketDataService, tick_queue: asyncio.Queue[TickEvent]) -> None:
        self._service = market_data_service
        self._queue = tick_queue
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                spot = await self._service.get_nifty_spot()
                tick = TickEvent(symbol='NIFTY', price=spot, timestamp=datetime.now(timezone.utc).isoformat())
                await self._queue.put(tick)
            except Exception:
                logger.warning('market_feed_error_continue', exc_info=True)
            await asyncio.sleep(settings.market_tick_interval_seconds)

    def stop(self) -> None:
        self._running = False
