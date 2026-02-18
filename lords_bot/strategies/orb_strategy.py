from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Helps static typing without import cycles
    from lords_bot.app.fyers_client import FyersClient

logger = logging.getLogger("lords_bot.strategy")

# India Standard Time zone
IST = ZoneInfo("Asia/Kolkata")


class ORBStrategy:
    """
    Opening Range Breakout (ORB) Strategy.

    Collects live ticks between 9:15–9:30 AM IST and
    locks range_high and range_low.

    After 9:30 AM, uses range to check breakout
    based on current LTP.
    """

    def __init__(self, client: "FyersClient", symbol: str = "NSE:NIFTY50-INDEX") -> None:
        self.client = client
        self.symbol = symbol

        self.range_high: float | None = None
        self.range_low: float | None = None
        self.live_ticks: list[float] = []
        self.range_locked = False

    def _current_ist_time(self) -> dt.time:
        """Get current time in IST."""
        return dt.datetime.now(tz=IST).time()

    async def on_new_tick(self, tick_data: dict[str, float]) -> None:
        """
        Called when a new WebSocket tick arrives.
        tick_data contains at least {"ltp": float}.
        """

        ltp = tick_data.get("ltp")
        if ltp is None:
            return

        now = self._current_ist_time()

        # Collect ticks during 9:15–9:30
        if dt.time(9, 15) <= now < dt.time(9, 30):
            self.live_ticks.append(ltp)

        # After 9:30 lock range once
        if now >= dt.time(9, 30) and not self.range_locked:
            if self.live_ticks:
                self.range_high = max(self.live_ticks)
                self.range_low = min(self.live_ticks)
                self.range_locked = True
                logger.info(
                    "ORB range locked for %s: High=%s Low=%s",
                    self.symbol,
                    self.range_high,
                    self.range_low,
                )
            else:
                logger.warning("No ticks collected for ORB range.")

    async def fetch_quote_ltp(self) -> float | None:
        """
        Fetch the current LTP from REST as a fallback when needed.
        Uses safe request; returns None on error.
        """
        try:
            response = await self.client.request(
                method="GET",
                endpoint="/quotes",
                params={"symbols": self.symbol},
            )

            # Fyers quotes payload
            # Most often under response["d"][0]["v"]["lp"]
            data_d = response.get("d")
            if isinstance(data_d, list) and data_d:
                v = data_d[0].get("v", {})
                raw_ltp = v.get("lp") or v.get("ltP")
                if raw_ltp is not None:
                    return float(raw_ltp)
        except Exception as exc:
            logger.warning("ORB fetch_quote_ltp failed: %s", exc)

        return None

    async def check_breakout(self) -> dict[str, object] | None:
        """
        Check for breakout relative to the locked ORB range.

        Returns:
            {"direction": "CALL"|"PUT", "price": ltp}
            OR None if no signal or no ORB range yet.
        """

        # Must have a range locked
        if not self.range_locked:
            logger.debug("ORB range not locked yet.")
            return None

        # Try getting LTP from REST fallback
        ltp = await self.fetch_quote_ltp()

        if ltp is None:
            logger.debug("ORB breakout check skipped — LTP unavailable.")
            return None

        # Breakout above range
        if self.range_high is not None and ltp > self.range_high:
            logger.info("ORB breakout CALL @ %s (above %s)", ltp, self.range_high)
            return {"direction": "CALL", "price": ltp}

        # Breakdown below range
        if self.range_low is not None and ltp < self.range_low:
            logger.info("ORB breakdown PUT @ %s (below %s)", ltp, self.range_low)
            return {"direction": "PUT", "price": ltp}

        # No signal
        return None
