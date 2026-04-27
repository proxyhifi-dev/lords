# backend/app/strategy/orb_strategy.py

from __future__ import annotations

from collections import deque
from datetime import datetime, time
from zoneinfo import ZoneInfo

from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import StateManager
from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger   = get_logger("orb_strategy")


class OrbStrategy:
    """
    Production-grade ORB strategy
    - VWAP filter
    - Volume spike filter
    - Multi-tick confirmation
    - Daily reset
    - Risk/state aware
    """

    def __init__(self, event_bus: EventBus, state_manager: StateManager) -> None:

        self.event_bus     = event_bus
        self.state_manager = state_manager

        self.orb_high: float | None = None
        self.orb_low: float | None = None

        self.frozen = False
        self.signal_emitted = False

        self._tick_window: deque[float] = deque(maxlen=5)
        self._volume_window: deque[float] = deque(maxlen=20)

        self._vwap_price_sum = 0.0
        self._vwap_volume_sum = 0.0

        self._last_signal_time = 0.0
        self.cooldown = 10

        self._tz = ZoneInfo("Asia/Kolkata")
        self._last_date = None

    async def run(self) -> None:

        queue = self.event_bus.subscribe("TICK")

        async for event in self.event_bus.iter_events(queue):

            now = datetime.now(self._tz)

            # ─────────────────────────────
            # DAILY RESET (CRITICAL FIX)
            # ─────────────────────────────
            today = now.date()
            if self._last_date != today:
                self._last_date = today

                self.orb_high = None
                self.orb_low = None
                self.frozen = False
                self.signal_emitted = False

                self._vwap_price_sum = 0.0
                self._vwap_volume_sum = 0.0

                self._tick_window.clear()
                self._volume_window.clear()

                logger.info("ORB reset for new day")

            tick = event.payload

            price = float(tick["price"])
            volume = float(tick.get("volume", 1))

            self._tick_window.append(price)
            self._volume_window.append(volume)

            # ─────────────────────────────
            # VWAP
            # ─────────────────────────────
            self._vwap_price_sum += price * volume
            self._vwap_volume_sum += volume

            vwap = (
                self._vwap_price_sum / self._vwap_volume_sum
                if self._vwap_volume_sum > 0
                else None
            )

            # ─────────────────────────────
            # STATE CHECK (CRITICAL FIX)
            # ─────────────────────────────
            state = await self.state_manager.snapshot()

            if not state.trading_enabled:
                continue

            if state.active_trade:
                continue

            # ─────────────────────────────
            # ORB BUILD WINDOW (CONFIG BASED)
            # ─────────────────────────────
            orb_start = getattr(settings, "orb_start", time(9, 15))
            orb_end   = getattr(settings, "orb_end",   time(9, 30))

            if orb_start <= now.time() < orb_end and not self.frozen:

                self.orb_high = price if self.orb_high is None else max(self.orb_high, price)
                self.orb_low  = price if self.orb_low  is None else min(self.orb_low,  price)

                await self.event_bus.publish(
                    "ORB_UPDATED",
                    {"orb_high": self.orb_high, "orb_low": self.orb_low},
                )
                continue

            # ─────────────────────────────
            # FREEZE ORB
            # ─────────────────────────────
            if now.time() >= orb_end and not self.frozen:

                self.frozen = True

                await self.event_bus.publish(
                    "ORB_FROZEN",
                    {"orb_high": self.orb_high, "orb_low": self.orb_low},
                )

            # ─────────────────────────────
            # BREAKOUT LOGIC
            # ─────────────────────────────
            if (
                self.frozen
                and not self.signal_emitted
                and self.orb_high is not None
                and self.orb_low is not None
                and len(self._tick_window) >= 5
            ):

                above = all(v > self.orb_high for v in self._tick_window)
                below = all(v < self.orb_low  for v in self._tick_window)

                # ✅ STRONGER BREAKOUT (0.2%)
                ce_break = price > self.orb_high and (
                    (price - self.orb_high) / self.orb_high
                ) >= 0.002

                pe_break = price < self.orb_low and (
                    (self.orb_low - price) / self.orb_low
                ) >= 0.002

                # ✅ STRICT VWAP FILTER
                vwap_long  = vwap is not None and price > vwap
                vwap_short = vwap is not None and price < vwap

                # Volume spike
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

                # ─────────────────────────────
                # SIGNAL GENERATION
                # ─────────────────────────────
                if ce_break and above and volume_spike and vwap_long:

                    self.signal_emitted = True
                    self._last_signal_time = now_ts

                    payload = {
                        "signal": "CALL",
                        "price": price,
                        "qty": settings.order_qty,
                        "timestamp": now.isoformat(),
                    }

                    logger.info("CALL SIGNAL")

                    await self.event_bus.publish("SIGNAL", payload)

                elif pe_break and below and volume_spike and vwap_short:

                    self.signal_emitted = True
                    self._last_signal_time = now_ts

                    payload = {
                        "signal": "PUT",
                        "price": price,
                        "qty": settings.order_qty,
                        "timestamp": now.isoformat(),
                    }

                    logger.info("PUT SIGNAL")

                    await self.event_bus.publish("SIGNAL", payload)