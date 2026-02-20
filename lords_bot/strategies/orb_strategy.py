from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lords_bot.app.fyers_client import FyersClient

logger = logging.getLogger("lords_bot.strategy")

IST = ZoneInfo("Asia/Kolkata")


class ORBStrategy:
    """
    Opening Range Breakout Strategy (REST based).

    1. Collects LTP between 9:15–9:30 IST
    2. Locks range at 9:30
    3. After 9:30 checks breakout
    """

    def __init__(
        self,
        client: "FyersClient",
        symbol: str = "NSE:NIFTY50-INDEX",
    ) -> None:
        self.client = client
        self.symbol = symbol

        self.range_high: float | None = None
        self.range_low: float | None = None
        self.range_locked: bool = False

        self.live_ticks: list[float] = []

    # ---------------------------------------------------------
    # Time helper
    # ---------------------------------------------------------

    def _current_ist_time(self) -> dt.time:
        return dt.datetime.now(tz=IST).time()

    # ---------------------------------------------------------
    # Tick handler (called by PollingService)
    # ---------------------------------------------------------

    async def on_new_tick(self, ltp: float) -> None:
        now = self._current_ist_time()

        # Collect ticks during ORB window
        if dt.time(9, 15) <= now < dt.time(9, 30):
            self.live_ticks.append(ltp)
            logger.debug("Collecting ORB tick: %s", ltp)

        # Lock range at 9:30
        if now >= dt.time(9, 30) and not self.range_locked:
            if not self.live_ticks:
                logger.warning("No ticks collected for ORB window.")
                return

            self.range_high = max(self.live_ticks)
            self.range_low = min(self.live_ticks)
            self.range_locked = True

            logger.info(
                "ORB Range Locked → HIGH=%s LOW=%s",
                self.range_high,
                self.range_low,
            )

    # ---------------------------------------------------------
    # REST LTP fetch (fallback)
    # ---------------------------------------------------------

    async def fetch_quote_ltp(self) -> float | None:
        try:
            response = await self.client.request(
                method="GET",
                endpoint="/quotes",
                params={"symbols": self.symbol},
            )

            data_d = response.get("d")
            if isinstance(data_d, list) and data_d:
                v = data_d[0].get("v", {})
                raw_ltp = v.get("lp") or v.get("ltP")
                if raw_ltp is not None:
                    return float(raw_ltp)

        except Exception as exc:
            logger.warning("Failed to fetch LTP: %s", exc)

        return None

    # ---------------------------------------------------------
    # Breakout Logic
    # ---------------------------------------------------------

    async def check_breakout(self) -> dict[str, object] | None:

        if not self.range_locked:
            return None

        ltp = await self.fetch_quote_ltp()
        if ltp is None:
            return None

        # Breakout above range
        if self.range_high is not None and ltp > self.range_high:
            logger.info("ORB BREAKOUT CALL @ %s", ltp)
            return {"direction": "CALL", "price": ltp}

        # Breakdown below range
        if self.range_low is not None and ltp < self.range_low:
            logger.info("ORB BREAKDOWN PUT @ %s", ltp)
            return {"direction": "PUT", "price": ltp}

        return None
