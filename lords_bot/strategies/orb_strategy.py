from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from lords_bot.app.fyers_client import FyersClient

logger = logging.getLogger("lords_bot.strategy")
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class TickState:
    samples: list[float] = field(default_factory=list)
    high: float | None = None
    low: float | None = None
    last_price: float | None = None


class ORBStrategy:
    def __init__(self, client: "FyersClient", symbol: str = "NSE:NIFTY50-INDEX") -> None:
        self.client = client
        self.symbol = symbol

        self.tick_state = TickState()
        self.range_date: dt.date | None = None
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.range_locked: bool = False

    def _current_ist_time(self) -> dt.time:
        return dt.datetime.now(tz=IST).time()

    async def on_new_tick(self, ltp: float) -> None:
        self.tick_state.last_price = ltp
        now = self._current_ist_time()

        if dt.time(9, 15) <= now < dt.time(9, 30):
            self.tick_state.samples.append(ltp)
            self.tick_state.high = max(self.tick_state.samples)
            self.tick_state.low = min(self.tick_state.samples)
            return

        if now >= dt.time(9, 30) and not self.range_locked and self.tick_state.samples:
            self.range_high = max(self.tick_state.samples)
            self.range_low = min(self.tick_state.samples)
            self.range_date = dt.date.today()
            self.range_locked = True

    async def fetch_quote_ltp(self) -> float | None:
        try:
            response = await self.client.request("GET", "/quotes", params={"symbols": self.symbol})
            data_d = response.get("d")
            if isinstance(data_d, list) and data_d:
                value = data_d[0].get("v", {}).get("lp")
                if value is not None:
                    return float(value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch LTP: %s", exc)
        return None

    async def check_breakout(self) -> dict[str, object] | None:
        # Compatibility: allow pre-seeded range from tests even if range_locked is False.
        if not self.range_locked and (self.range_high is None or self.range_low is None):
            return None

        ltp = self.tick_state.last_price if self.tick_state.last_price is not None else await self.fetch_quote_ltp()
        if ltp is None:
            return None

        if self.range_high is not None and ltp > self.range_high:
            return {"direction": "CALL", "price": ltp}
        if self.range_low is not None and ltp < self.range_low:
            return {"direction": "PUT", "price": ltp}
        return None
