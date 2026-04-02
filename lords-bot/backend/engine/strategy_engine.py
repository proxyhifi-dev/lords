from __future__ import annotations

import asyncio

from engine.market_feed_engine import TickEvent
from strategies.orb_strategy import OrbStrategy, TradeSignal


class StrategyEngine:
    def __init__(self, strategy: OrbStrategy, tick_queue: asyncio.Queue[TickEvent], signal_queue: asyncio.Queue[TradeSignal]) -> None:
        self._strategy = strategy
        self._tick_queue = tick_queue
        self._signal_queue = signal_queue
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            tick = await self._tick_queue.get()
            signal = self._strategy.on_tick(tick.price, qty=50)
            if signal:
                await self._signal_queue.put(signal)

    def stop(self) -> None:
        self._running = False
