from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class ORBStrategy:
    def __init__(self, client) -> None:
        self.client = client
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.range_date: dt.date | None = None

    async def fetch_orb_range(self) -> dict[str, float]:
        today_ist = dt.datetime.now(tz=IST).date()
        day = today_ist.strftime("%Y-%m-%d")
        response = await self.client.request(
            "GET",
            "/history",
            params={
                "symbol": "NSE:NIFTY50-INDEX",
                "resolution": "5",
                "date_format": "1",
                "range_from": day,
                "range_to": day,
                "cont_flag": "1",
            },
        )
        candles: list[list[float]] = response.get("candles", [])
        if not candles:
            raise RuntimeError("No NIFTY candles returned from FYERS history endpoint.")

        orb_highs: list[float] = []
        orb_lows: list[float] = []
        for candle in candles:
            ts = int(candle[0])
            candle_time = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(IST).time()
            if dt.time(9, 15) <= candle_time < dt.time(9, 30):
                orb_highs.append(float(candle[2]))
                orb_lows.append(float(candle[3]))

        if not orb_highs or not orb_lows:
            raise RuntimeError("Unable to compute ORB range for 9:15-9:30 IST.")

        self.range_high = max(orb_highs)
        self.range_low = min(orb_lows)
        self.range_date = today_ist
        logger.info("ORB range computed high=%s low=%s", self.range_high, self.range_low)
        return {"high": self.range_high, "low": self.range_low}

    async def check_breakout(self) -> dict[str, float | str] | None:
        today_ist = dt.datetime.now(tz=IST).date()
        if self.range_date != today_ist or self.range_high is None or self.range_low is None:
            await self.fetch_orb_range()

        quote = await self.client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"})
        ltp = float(quote["d"][0]["v"]["lp"])

        if ltp > float(self.range_high):
            return {"direction": "CALL", "price": ltp, "range_high": float(self.range_high), "range_low": float(self.range_low)}
        if ltp < float(self.range_low):
            return {"direction": "PUT", "price": ltp, "range_high": float(self.range_high), "range_low": float(self.range_low)}
        return None
