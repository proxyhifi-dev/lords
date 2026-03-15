from __future__ import annotations

from datetime import datetime, time
from typing import Any


class CandleBuilder:
    def build_5min_candles(self, raw_ticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return raw_ticks

    def opening_range(self, candles: list[dict[str, Any]]) -> dict[str, float]:
        start = time(9, 15)
        end = time(9, 45)
        window = [
            c for c in candles if start <= datetime.fromisoformat(c['timestamp']).time() <= end
        ]
        if not window:
            return {'high': 0.0, 'low': 0.0}
        return {
            'high': max(float(c['high']) for c in window),
            'low': min(float(c['low']) for c in window),
        }
