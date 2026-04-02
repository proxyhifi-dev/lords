from __future__ import annotations

import asyncio
import time


class GlobalRateLimiter:
    """Global async rate limiter enforcing minimum interval between API calls."""

    def __init__(self, min_interval_seconds: float = 2.0) -> None:
        self._min_interval_seconds = max(2.0, min_interval_seconds)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last_call
            if delta < self._min_interval_seconds:
                await asyncio.sleep(self._min_interval_seconds - delta)
            self._last_call = time.monotonic()
