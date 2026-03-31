from __future__ import annotations

from datetime import datetime
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

        first_15 = candles[:15]

        highs = [c["high"] for c in first_15]
        lows = [c["low"] for c in first_15]

        return {
            "high": max(highs),
            "low": min(lows),
        }