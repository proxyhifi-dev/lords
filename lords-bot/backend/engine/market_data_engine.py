from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from services.market_data_service import MarketDataService

IST = ZoneInfo('Asia/Kolkata')


@dataclass
class TickEvent:
    timestamp: datetime
    price: float
    volume: float = 0.0


class TickStream:
    def __init__(self, maxsize: int = 2048) -> None:
        self._queue: asyncio.Queue[TickEvent] = asyncio.Queue(maxsize=maxsize)

    async def publish(self, event: TickEvent) -> None:
        if self._queue.full():
            _ = self._queue.get_nowait()
        await self._queue.put(event)

    async def subscribe(self) -> AsyncIterator[TickEvent]:
        while True:
            yield await self._queue.get()


class MarketFeedEngine:
    def __init__(self, market_data_service: MarketDataService) -> None:
        self.market_data_service = market_data_service
        self.tick_stream = TickStream()
        self._tick_buffer: deque[TickEvent] = deque(maxlen=10000)
        self._running = False
        self._task: asyncio.Task | None = None

    async def _run(self, interval_seconds: float) -> None:
        while self._running:
            spot = await self.market_data_service.get_spot_price()
            event = TickEvent(timestamp=datetime.now(IST), price=spot)
            self._tick_buffer.append(event)
            self.market_data_service.add_tick(spot, event.timestamp)
            await self.tick_stream.publish(event)
            await asyncio.sleep(interval_seconds)

    async def start(self, interval_seconds: float) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(interval_seconds))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
