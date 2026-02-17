import asyncio
import time
from collections import deque


class RateLimiter:
    """Sliding-window limiter for per-second and per-minute request caps."""

    def __init__(self, per_second: int = 10, per_minute: int = 200) -> None:
        self.per_second = per_second
        self.per_minute = per_minute
        self._per_second_calls: deque[float] = deque()
        self._per_minute_calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._evict_old(now)

                if (
                    len(self._per_second_calls) < self.per_second
                    and len(self._per_minute_calls) < self.per_minute
                ):
                    self._per_second_calls.append(now)
                    self._per_minute_calls.append(now)
                    return

                sleep_for = self._sleep_time(now)
                await asyncio.sleep(max(sleep_for, 0.01))

    def _evict_old(self, now: float) -> None:
        while self._per_second_calls and now - self._per_second_calls[0] >= 1:
            self._per_second_calls.popleft()
        while self._per_minute_calls and now - self._per_minute_calls[0] >= 60:
            self._per_minute_calls.popleft()

    def _sleep_time(self, now: float) -> float:
        wait_second = 0.0
        wait_minute = 0.0

        if len(self._per_second_calls) >= self.per_second:
            wait_second = 1 - (now - self._per_second_calls[0])
        if len(self._per_minute_calls) >= self.per_minute:
            wait_minute = 60 - (now - self._per_minute_calls[0])

        return max(wait_second, wait_minute)
