from __future__ import annotations
import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("lords_bot.polling")


class PollingService:
    """
    Production-safe REST polling service.
    Replaces WebSocket completely.
    """

    def __init__(self, client):
        self.client = client
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(
        self,
        symbol: str,
        interval: float,
        on_tick: Callable[[float], Awaitable[None]],
    ) -> None:
        if self._running:
            logger.warning("Polling already running.")
            return

        self._running = True
        self._task = asyncio.create_task(
            self._loop(symbol, interval, on_tick)
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Polling stopped.")

    async def _loop(
        self,
        symbol: str,
        interval: float,
        on_tick: Callable[[float], Awaitable[None]],
    ) -> None:
        while self._running:
            try:
                quote = await self.client.request(
                    "GET",
                    "/quotes",
                    params={"symbols": symbol},
                )

                ltp = float(quote["d"][0]["v"]["lp"])
                await on_tick(ltp)

            except Exception as e:
                logger.warning("Polling error: %s", e)

            await asyncio.sleep(interval)
