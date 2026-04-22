from __future__ import annotations
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.core.event_bus import EventBus


class OrbStrategy:
    """
    Standalone ORB strategy with VWAP + volume-spike filters.

    Wire this into MarketScheduler if you want the richer signal logic.
    Currently the scheduler has its own lighter breakout detector.
    This class is preserved as an optional upgrade path.
    """

    def __init__(self, event_bus: EventBus) -> None:

        self.event_bus = event_bus

        self.orb_high: float | None = None
        self.orb_low: float | None = None

        self.frozen = False
        self.signal_emitted = False

        self._tick_window: deque[float] = deque(maxlen=5)
        self._volume_window: deque[float] = deque(maxlen=20)

        self._vwap_price_sum = 0.0
        self._vwap_volume_sum = 0.0

        self._last_signal_time = 0.0
        self.cooldown = 10                    # seconds between signals
        self._tz = ZoneInfo("Asia/Kolkata")

    async def run(self) -> None:

        queue = self.event_bus.subscribe("TICK")

        async for event in self.event_bus.iter_events(queue):

            tick = event.payload
            now = datetime.now(self._tz)

            price = float(tick["price"])
            volume = float(tick.get("volume", 1))

            self._tick_window.append(price)
            self._volume_window.append(volume)

            # ── VWAP ─────────────────────────────────────────
            self._vwap_price_sum += price * volume
            self._vwap_volume_sum += volume
            vwap = (
                self._vwap_price_sum / self._vwap_volume_sum
                if self._vwap_volume_sum > 0
                else None
            )

            # ── BUILD ORB  09:15–09:30 ────────────────────────
            if now.hour == 9 and 15 <= now.minute < 30 and not self.frozen:

                self.orb_high = price if self.orb_high is None else max(self.orb_high, price)
                self.orb_low  = price if self.orb_low  is None else min(self.orb_low,  price)

                await self.event_bus.publish(
                    "ORB_UPDATED",
                    {"orb_high": self.orb_high, "orb_low": self.orb_low},
                )
                continue

            # ── FREEZE ORB ────────────────────────────────────
            if (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and not self.frozen:

                self.frozen = True
                await self.event_bus.publish(
                    "ORB_FROZEN",
                    {"orb_high": self.orb_high, "orb_low": self.orb_low},
                )

            # ── BREAKOUT DETECTION ────────────────────────────
            if (
                self.frozen
                and not self.signal_emitted
                and self.orb_high is not None
                and self.orb_low is not None
                and len(self._tick_window) >= 5
            ):
                # False-breakout filter: all 5 recent ticks must be outside range
                above = all(v > self.orb_high for v in self._tick_window)
                below = all(v < self.orb_low  for v in self._tick_window)

                # 0.1 % buffer beyond range
                ce_break = price > self.orb_high and (
                    (price - self.orb_high) / self.orb_high
                ) >= 0.001
                pe_break = price < self.orb_low and (
                    (self.orb_low - price) / self.orb_low
                ) >= 0.001

                # VWAP direction filter
                vwap_long  = vwap is None or price > vwap
                vwap_short = vwap is None or price < vwap

                # Volume spike filter  (1.5× average)
                avg_volume = (
                    sum(self._volume_window) / len(self._volume_window)
                    if self._volume_window
                    else 0
                )
                volume_spike = volume > (avg_volume * 1.5) if avg_volume > 0 else True

                # Cooldown
                now_ts = now.timestamp()
                if now_ts - self._last_signal_time < self.cooldown:
                    continue

                # ── CALL ──────────────────────────────────────
                if ce_break and above and volume_spike and vwap_long:

                    self.signal_emitted = True
                    self._last_signal_time = now_ts

                    await self.event_bus.publish(
                        "SIGNAL",
                        {"signal": "CALL", "spot_price": price, "vwap": vwap},
                    )

                # ── PUT ───────────────────────────────────────
                elif pe_break and below and volume_spike and vwap_short:

                    self.signal_emitted = True
                    self._last_signal_time = now_ts

                    await self.event_bus.publish(
                        "SIGNAL",
                        {"signal": "PUT", "spot_price": price, "vwap": vwap},
                    )
