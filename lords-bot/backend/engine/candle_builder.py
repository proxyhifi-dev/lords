from __future__ import annotations

from datetime import datetime, time
from typing import Any


class CandleBuilder:

    def __init__(self) -> None:

        self._ticks: list[dict[str, Any]] = []

    def reset_for_session(self, session_date) -> None:

        self._ticks = []

    def add_tick(self, tick: dict[str, Any]) -> None:

        ts = tick.get("timestamp")
        price = float(tick.get("price") or 0)

        if not ts or price <= 0:
            return

        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)

        self._ticks.append(
            {
                "timestamp": ts,
                "price": price,
                "volume": float(tick.get("volume") or 0),
            }
        )

    def prune_ticks(self) -> None:

        if len(self._ticks) > 10000:
            self._ticks = self._ticks[-5000:]

    # ------------------------------------------------
    # 5 MINUTE CANDLES
    # ------------------------------------------------

    def build_5min_candles(self, ticks: list[dict[str, Any]] | None = None):

        source = ticks if ticks is not None else self._ticks

        if not source:
            return []

        candles: dict[datetime, dict[str, Any]] = {}

        for tick in source:
            ts = tick.get("timestamp")
            price = float(tick.get("price") or 0)
            if not ts or price <= 0:
                continue

            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)

            minute_bucket = (ts.minute // 5) * 5
            bucket = ts.replace(minute=minute_bucket, second=0, microsecond=0)

            candle = candles.get(bucket)
            if candle is None:
                candles[bucket] = {
                    "timestamp": bucket.isoformat(),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": float(tick.get("volume") or 0),
                }
                continue

            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += float(tick.get("volume") or 0)

        return [candles[k] for k in sorted(candles.keys())]

    # ------------------------------------------------
    # 1 MINUTE CANDLES
    # ------------------------------------------------

    def build_1min_candles(self):

        if not self._ticks:
            return []

        candles = {}

        for tick in self._ticks:

            ts = tick["timestamp"]

            bucket = ts.replace(second=0, microsecond=0)

            price = tick["price"]
            volume = tick["volume"]

            candle = candles.get(bucket)

            if candle is None:

                candles[bucket] = {
                    "timestamp": bucket.isoformat(),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                }

                continue

            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += volume

        return [candles[k] for k in sorted(candles.keys())]

    # ------------------------------------------------
    # ORB RANGE
    # ------------------------------------------------

    def opening_range(self, candles):

        if not candles:
            return {"high": 0.0, "low": 0.0}

        window: list[dict[str, Any]] = []
        for candle in candles:
            ts = candle.get("timestamp")
            if not ts:
                continue
            dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
            if time(9, 15) <= dt.time() < time(9, 30):
                window.append(candle)

        if not window:
            return {"high": 0.0, "low": 0.0}

        highs = [c["high"] for c in window]
        lows = [c["low"] for c in window]

        return {
            "high": max(highs),
            "low": min(lows),
        }
