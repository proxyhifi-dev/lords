from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any


class CandleBuilder:
    def __init__(self) -> None:
        self._ticks: list[dict[str, Any]] = []
        self._active_session: date | None = None

    def reset_for_session(self, session_date: date) -> None:
        if self._active_session != session_date:
            self._ticks = []
            self._active_session = session_date

    def add_tick(self, tick: dict[str, Any]) -> None:
        """
        Add tick data from websocket or market feed
        """
        ts_value = tick.get("timestamp")
        if not ts_value:
            return

        try:
            timestamp = ts_value if isinstance(ts_value, datetime) else datetime.fromisoformat(str(ts_value))
        except Exception:
            return

        price = float(tick.get("price") or tick.get("ltp") or 0.0)
        volume = float(tick.get("volume") or 0.0)

        if price <= 0:
            return

        self.reset_for_session(timestamp.date())

        self._ticks.append(
            {
                "timestamp": timestamp,
                "price": price,
                "volume": volume,
            }
        )

    def build_5min_candles(self, raw_ticks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """
        Convert ticks → 5 minute candles
        """
        source = raw_ticks if raw_ticks is not None else self._ticks

        if not source:
            return []

        parsed: list[dict[str, Any]] = []

        for tick in source:
            ts_value = tick.get("timestamp")
            if not ts_value:
                continue

            try:
                timestamp = ts_value if isinstance(ts_value, datetime) else datetime.fromisoformat(str(ts_value))
            except Exception:
                continue

            price = float(tick.get("price") or tick.get("ltp") or 0.0)
            volume = float(tick.get("volume") or 0.0)

            if price <= 0:
                continue

            parsed.append(
                {
                    "timestamp": timestamp,
                    "price": price,
                    "volume": volume,
                }
            )

        if not parsed:
            return []

        parsed.sort(key=lambda x: x["timestamp"])

        candles: list[dict[str, Any]] = []

        current_bucket: datetime | None = None
        current: dict[str, Any] = {}

        for tick in parsed:
            ts = tick["timestamp"]

            bucket = ts.replace(
                minute=(ts.minute // 5) * 5,
                second=0,
                microsecond=0,
            )

            if current_bucket != bucket:
                if current:
                    candles.append(current)

                current_bucket = bucket

                current = {
                    "timestamp": bucket.isoformat(),
                    "open": tick["price"],
                    "high": tick["price"],
                    "low": tick["price"],
                    "close": tick["price"],
                    "volume": tick["volume"],
                }

                continue

            current["high"] = max(float(current["high"]), tick["price"])
            current["low"] = min(float(current["low"]), tick["price"])
            current["close"] = tick["price"]
            current["volume"] = float(current["volume"]) + tick["volume"]

        if current:
            candles.append(current)

        return candles

    def prune_ticks(self, keep_minutes: int = 120) -> None:
        """
        Remove old ticks to prevent memory growth
        """
        if not self._ticks:
            return

        latest = max(t["timestamp"] for t in self._ticks)
        cutoff = latest - timedelta(minutes=keep_minutes)

        self._ticks = [t for t in self._ticks if t["timestamp"] >= cutoff]

    def opening_range(self, candles: list[dict[str, Any]]) -> dict[str, float]:
        """
        Calculate ORB range from 09:15 → 09:45
        """

        if not candles:
            return {"high": 0.0, "low": 0.0}

        parsed = []

        for c in candles:
            try:
                ts = datetime.fromisoformat(str(c["timestamp"]))
                parsed.append((ts, c))
            except Exception:
                continue

        if not parsed:
            return {"high": 0.0, "low": 0.0}

        session_date = max(ts.date() for ts, _ in parsed)

        window = [
            c
            for ts, c in parsed
            if ts.date() == session_date and time(9, 15) <= ts.time() < time(9, 45)
        ]

        # fallback if ORB window candles missing
        if not window:
            first = candles[:6]

            if not first:
                return {"high": 0.0, "low": 0.0}

            return {
                "high": max(float(c["high"]) for c in first),
                "low": min(float(c["low"]) for c in first),
            }

        return {
            "high": max(float(c["high"]) for c in window),
            "low": min(float(c["low"]) for c in window),
        }
