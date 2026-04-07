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

        # ULTRA PRO ADDITIONS
        self._volume_window: deque[float] = deque(maxlen=20)
        self._vwap_price_sum = 0
        self._vwap_volume_sum = 0

        self._last_signal_time = 0
        self.cooldown = 10

    async def run(self) -> None:

        queue = self.event_bus.subscribe("TICK")

        async for event in self.event_bus.iter_events(queue):

            tick = event.payload

            now = datetime.now()

            price = float(tick["price"])

            volume = float(tick.get("volume", 1))

            self._tick_window.append(price)

            self._volume_window.append(volume)

            # -------------------------------------
            # VWAP CALCULATION
            # -------------------------------------

            self._vwap_price_sum += price * volume
            self._vwap_volume_sum += volume

            vwap = None

            if self._vwap_volume_sum > 0:
                vwap = self._vwap_price_sum / self._vwap_volume_sum

            # -------------------------------------
            # BUILD ORB RANGE
            # -------------------------------------

            if now.hour == 9 and 15 <= now.minute < 30 and not self.frozen:

                self.orb_high = price if self.orb_high is None else max(self.orb_high, price)
                self.orb_low = price if self.orb_low is None else min(self.orb_low, price)

                await self.event_bus.publish(
                    "ORB_UPDATED",
                    {
                        "orb_high": self.orb_high,
                        "orb_low": self.orb_low,
                    },
                )

                continue

            # -------------------------------------
            # FREEZE ORB
            # -------------------------------------

            if (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and not self.frozen:

                self.frozen = True

                await self.event_bus.publish(
                    "ORB_FROZEN",
                    {
                        "orb_high": self.orb_high,
                        "orb_low": self.orb_low,
                    },
                )

            # -------------------------------------
            # BREAKOUT LOGIC
            # -------------------------------------

            if self.frozen and not self.signal_emitted and self.orb_high is not None and self.orb_low is not None:

                if len(self._tick_window) < 5:
                    continue

                # FALSE BREAKOUT FILTER
                above = all(v > self.orb_high for v in self._tick_window)
                below = all(v < self.orb_low for v in self._tick_window)

                # BUFFER BREAKOUT
                ce_break = price > self.orb_high and ((price - self.orb_high) / self.orb_high) >= 0.001
                pe_break = price < self.orb_low and ((self.orb_low - price) / self.orb_low) >= 0.001

                # -------------------------------------
                # VWAP FILTER
                # -------------------------------------

                vwap_long = vwap is None or price > vwap
                vwap_short = vwap is None or price < vwap

                # -------------------------------------
                # VOLUME SPIKE FILTER
                # -------------------------------------

                avg_volume = sum(self._volume_window) / len(self._volume_window)

                volume_spike = volume > (avg_volume * 1.5)

                # -------------------------------------
                # COOLDOWN
                # -------------------------------------

                now_ts = now.timestamp()

                if now_ts - self._last_signal_time < self.cooldown:
                    continue

                # -------------------------------------
                # CALL SIGNAL
                # -------------------------------------

                if ce_break and above and volume_spike and vwap_long:

                    self.signal_emitted = True
                    self._last_signal_time = now_ts

                    await self.event_bus.publish(
                        "SIGNAL",
                        {
                            "signal": "CALL",
                            "spot_price": price,
                            "vwap": vwap,
                        },
                    )

                # -------------------------------------
                # PUT SIGNAL
                # -------------------------------------

                elif pe_break and below and volume_spike and vwap_short:

                    self.signal_emitted = True
                    self._last_signal_time = now_ts

                    await self.event_bus.publish(
                        "SIGNAL",
                        {
                            "signal": "PUT",
                            "spot_price": price,
                            "vwap": vwap,
                        },
                    )