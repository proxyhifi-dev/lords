from __future__ import annotations

import asyncio
import time


class GlobalRateLimiter:
    def __init__(self, calls_per_second: float) -> None:
        self._min_interval = 1.0 / max(0.1, calls_per_second)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_for = self._min_interval - (now - self._last_call)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_call = time.monotonic()
