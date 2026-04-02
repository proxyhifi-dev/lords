from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


Signal = Literal["BUY_CE", "BUY_PE"]


@dataclass
class OrbState:
    orb_high: float | None = None
    orb_low: float | None = None
    is_frozen: bool = False
    signal: Signal | None = None


class OrbStrategy:
    def __init__(self) -> None:
        self.state = OrbState()
        self._last_ticks: deque[dict] = deque(maxlen=5)
        self._volume_window: deque[float] = deque(maxlen=20)

    @staticmethod
    def _in_orb_window(now: datetime) -> bool:
        return now.hour == 9 and 15 <= now.minute < 30

    def update_orb(self, tick: dict, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self._last_ticks.append(tick)
        self._volume_window.append(float(tick.get("volume") or 0))

        if self._in_orb_window(now):
            price = float(tick["price"])
            self.state.orb_high = price if self.state.orb_high is None else max(self.state.orb_high, price)
            self.state.orb_low = price if self.state.orb_low is None else min(self.state.orb_low, price)
        if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
            self.state.is_frozen = True

    def check_breakout(self, tick: dict, now: datetime | None = None) -> Signal | None:
        now = now or datetime.now()
        if not self.state.is_frozen or self.state.orb_high is None or self.state.orb_low is None:
            return None
        if self.state.signal is not None:
            return None

        price = float(tick["price"])
        if len(self._last_ticks) < 5:
            return None

        vol = float(tick.get("volume") or 0)
        avg_vol = (sum(self._volume_window) / len(self._volume_window)) if self._volume_window else 0
        volume_expansion = avg_vol == 0 or vol >= (1.2 * avg_vol)

        ce_break = price > self.state.orb_high and ((price - self.state.orb_high) / self.state.orb_high) >= 0.001
        pe_break = price < self.state.orb_low and ((self.state.orb_low - price) / self.state.orb_low) >= 0.001

        last5 = [float(t["price"]) for t in self._last_ticks]
        if ce_break and all(p > self.state.orb_high for p in last5) and volume_expansion:
            self.state.signal = "BUY_CE"
            return self.state.signal
        if pe_break and all(p < self.state.orb_low for p in last5):
            self.state.signal = "BUY_PE"
            return self.state.signal
        return None
