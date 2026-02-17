from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class ORBStrategy:
    """
    Opening Range Breakout Strategy for NIFTY 50

    - Computes ORB range from 9:15 to 9:30 IST
    - Detects breakout above high → CALL
    - Detects breakout below low → PUT
    """

    def __init__(self, client) -> None:
        self.client = client
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_date: Optional[dt.date] = None

    # ---------------------------------------------------------
    # Fetch ORB Range
    # ---------------------------------------------------------

    async def fetch_orb_range(self) -> Dict[str, float]:
        today_ist = dt.datetime.now(tz=IST).date()
        day = today_ist.strftime("%Y-%m-%d")

        response = await self.client.request(
            "GET",
            "/history",  # ✅ Correct FYERS endpoint
            params={
                "symbol": "NSE:NIFTY50-INDEX",
                "resolution": "5",
                "date_format": "1",
                "range_from": day,
                "range_to": day,
                "cont_flag": "1",
            },
        )

        candles: List[List[float]] = response.get("candles", [])

        if not candles:
            raise RuntimeError("No NIFTY candles returned from FYERS history endpoint.")

        orb_highs: List[float] = []
        orb_lows: List[float] = []

        for candle in candles:
            try:
                ts = int(candle[0])
                high = float(candle[2])
                low = float(candle[3])

                candle_time = (
                    dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
                    .astimezone(IST)
                    .time()
                )

                # ORB window: 9:15 to 9:30 IST
                if dt.time(9, 15) <= candle_time < dt.time(9, 30):
                    orb_highs.append(high)
                    orb_lows.append(low)

            except Exception:
                continue  # Skip malformed candles safely

        if not orb_highs or not orb_lows:
            raise RuntimeError("Unable to compute ORB range (market may not have opened yet).")

        self.range_high = max(orb_highs)
        self.range_low = min(orb_lows)
        self.range_date = today_ist

        logger.info(
            "ORB range computed | High=%s | Low=%s",
            self.range_high,
            self.range_low,
        )

        return {"high": self.range_high, "low": self.range_low}

    # ---------------------------------------------------------
    # Breakout Detection
    # ---------------------------------------------------------

    async def check_breakout(self) -> Optional[Dict[str, Any]]:
        today_ist = dt.datetime.now(tz=IST).date()

        # Recalculate ORB if not computed or new day
        if (
            self.range_date != today_ist
            or self.range_high is None
            or self.range_low is None
        ):
            await self.fetch_orb_range()

        # Get current LTP
        quote = await self.client.request(
            "GET",
            "/quotes",
            params={"symbols": "NSE:NIFTY50-INDEX"},
        )

        ltp = float(quote["d"][0]["v"]["lp"])

        logger.info("Current NIFTY LTP=%s", ltp)

        # Breakout Above
        if ltp > float(self.range_high):
            logger.info("ORB breakout above detected.")
            return {
                "direction": "CALL",
                "price": ltp,
                "range_high": float(self.range_high),
                "range_low": float(self.range_low),
            }

        # Breakout Below
        if ltp < float(self.range_low):
            logger.info("ORB breakdown detected.")
            return {
                "direction": "PUT",
                "price": ltp,
                "range_high": float(self.range_high),
                "range_low": float(self.range_low),
            }

        return None
