from __future__ import annotations

from collections import deque
from datetime import datetime

from backend.app.core.event_bus import EventBus


class OrbStrategy:
    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self.orb_high: float | None = None
        self.orb_low: float | None = None
        self.frozen = False
        self.signal_emitted = False
        self._tick_window: deque[float] = deque(maxlen=5)

    async def run(self) -> None:
        queue = self.event_bus.subscribe("TICK")
        async for event in self.event_bus.iter_events(queue):
            tick = event.payload
            now = datetime.now()
            price = float(tick["price"])
            self._tick_window.append(price)

            if now.hour == 9 and 15 <= now.minute < 30 and not self.frozen:
                self.orb_high = price if self.orb_high is None else max(self.orb_high, price)
                self.orb_low = price if self.orb_low is None else min(self.orb_low, price)
                await self.event_bus.publish("ORB_UPDATED", {"orb_high": self.orb_high, "orb_low": self.orb_low})
                continue

            if (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and not self.frozen:
                self.frozen = True
                await self.event_bus.publish("ORB_FROZEN", {"orb_high": self.orb_high, "orb_low": self.orb_low})

            if self.frozen and not self.signal_emitted and self.orb_high is not None and self.orb_low is not None:
                if len(self._tick_window) < 5:
                    continue

                above = all(v > self.orb_high for v in self._tick_window)
                below = all(v < self.orb_low for v in self._tick_window)
                ce_break = price > self.orb_high and ((price - self.orb_high) / self.orb_high) >= 0.001
                pe_break = price < self.orb_low and ((self.orb_low - price) / self.orb_low) >= 0.001

                if ce_break and above:
                    self.signal_emitted = True
                    await self.event_bus.publish("SIGNAL", {"signal": "BUY_CE", "spot_price": price})
                elif pe_break and below:
                    self.signal_emitted = True
                    await self.event_bus.publish("SIGNAL", {"signal": "BUY_PE", "spot_price": price})
