from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any
from zoneinfo import ZoneInfo

strategy_logger = logging.getLogger("lords_bot.strategy")
IST = ZoneInfo("Asia/Kolkata")


class ORBStrategy:
    """NIFTY50 ORB strategy with time, volume, and volatility filters."""

    def __init__(self, client) -> None:
        self.client = client
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.range_date: dt.date | None = None
        self.orb_avg_volume: float | None = None

        self.volume_multiplier = float(os.getenv("ORB_VOLUME_MULTIPLIER", "1.1"))
        self.atr_period = int(os.getenv("ORB_ATR_PERIOD", "14"))
        self.min_breakout_atr_ratio = float(os.getenv("ORB_MIN_BREAKOUT_ATR_RATIO", "0.2"))

    @staticmethod
    def _in_trading_window(now_ist: dt.datetime) -> bool:
        return dt.time(9, 15) <= now_ist.time() <= dt.time(15, 15)

    async def fetch_orb_range(self) -> dict[str, float] | None:
        today_ist = dt.datetime.now(tz=IST).date()
        day = today_ist.strftime("%Y-%m-%d")

        try:
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
        except Exception as exc:
            strategy_logger.exception("Failed to fetch ORB history: %s", exc)
            return None

        candles: list[list[float]] = response.get("candles", [])
        if not candles:
            strategy_logger.warning("No candles returned for ORB range computation.")
            return None

        orb_highs: list[float] = []
        orb_lows: list[float] = []
        orb_volumes: list[float] = []

        for candle in candles:
            try:
                ts = int(candle[0])
                high = float(candle[2])
                low = float(candle[3])
                volume = float(candle[5])
                candle_time = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(IST).time()
                if dt.time(9, 15) <= candle_time < dt.time(9, 30):
                    orb_highs.append(high)
                    orb_lows.append(low)
                    orb_volumes.append(volume)
            except (IndexError, TypeError, ValueError):
                continue

        if not orb_highs or not orb_lows:
            strategy_logger.info("ORB candles unavailable for current session.")
            return None

        self.range_high = max(orb_highs)
        self.range_low = min(orb_lows)
        self.orb_avg_volume = sum(orb_volumes) / len(orb_volumes) if orb_volumes else None
        self.range_date = today_ist

        return {"high": self.range_high, "low": self.range_low}

    @staticmethod
    def _compute_atr(candles: list[list[float]], period: int) -> float | None:
        if len(candles) < period + 1:
            return None

        trs: list[float] = []
        for idx in range(1, len(candles)):
            prev_close = float(candles[idx - 1][4])
            high = float(candles[idx][2])
            low = float(candles[idx][3])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        if len(trs) < period:
            return None
        recent = trs[-period:]
        return sum(recent) / len(recent)

    async def check_breakout(self) -> dict[str, Any] | None:
        now_ist = dt.datetime.now(tz=IST)
        if not self._in_trading_window(now_ist):
            return None

        try:
            if self.range_date != now_ist.date() or self.range_high is None or self.range_low is None:
                orb = await self.fetch_orb_range()
                if not orb:
                    return None

            day = now_ist.strftime("%Y-%m-%d")
            hist = await self.client.request(
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
            candles: list[list[float]] = hist.get("candles", [])
            if not candles:
                return None

            last_candle = candles[-1]
            last_volume = float(last_candle[5])
            atr = self._compute_atr(candles, self.atr_period)
            if atr is None:
                return None

            quote = await self.client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"})
            ltp = float(quote["d"][0]["v"]["lp"])

            breakout_size_up = ltp - float(self.range_high)
            breakout_size_down = float(self.range_low) - ltp
            volume_ok = self.orb_avg_volume is not None and last_volume >= (self.orb_avg_volume * self.volume_multiplier)

            if not volume_ok:
                return None

            if breakout_size_up > 0 and breakout_size_up >= atr * self.min_breakout_atr_ratio:
                return {
                    "direction": "CALL",
                    "price": ltp,
                    "range_high": float(self.range_high),
                    "range_low": float(self.range_low),
                    "atr": atr,
                    "last_volume": last_volume,
                }

            if breakout_size_down > 0 and breakout_size_down >= atr * self.min_breakout_atr_ratio:
                return {
                    "direction": "PUT",
                    "price": ltp,
                    "range_high": float(self.range_high),
                    "range_low": float(self.range_low),
                    "atr": atr,
                    "last_volume": last_volume,
                }

            return None
        except Exception as exc:
            strategy_logger.exception("ORB breakout check failed safely: %s", exc)
            return None
