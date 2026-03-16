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
        Convert ticks →
