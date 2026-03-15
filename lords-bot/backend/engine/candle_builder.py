from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any


class CandleBuilder:
    def __init__(self) -> None:
        self._ticks: list[dict[str, Any]] = []

    def add_tick(self, tick: dict[str, Any]) -> None:
        ts_value = tick.get('timestamp')
        if not ts_value:
            return
        price = float(tick.get('price') or tick.get('ltp') or 0.0)
        volume = float(tick.get('volume') or 0.0)
        if price <= 0:
            return
        timestamp = ts_value if isinstance(ts_value, datetime) else datetime.fromisoformat(str(ts_value))
        self._ticks.append({'timestamp': timestamp, 'price': price, 'volume': volume})

    def build_5min_candles(self, raw_ticks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        source = raw_ticks if raw_ticks is not None else self._ticks
        if not source:
            return []

        parsed: list[dict[str, Any]] = []
        for tick in source:
            ts_value = tick.get('timestamp')
            if not ts_value:
                continue
            timestamp = ts_value if isinstance(ts_value, datetime) else datetime.fromisoformat(str(ts_value))
            price = float(tick.get('price') or tick.get('ltp') or 0.0)
            volume = float(tick.get('volume') or 0.0)
            if price <= 0:
                continue
            parsed.append({'timestamp': timestamp, 'price': price, 'volume': volume})

        if not parsed:
            return []

        parsed.sort(key=lambda x: x['timestamp'])
        candles: list[dict[str, Any]] = []
        current_bucket: datetime | None = None
        current: dict[str, Any] = {}

        for tick in parsed:
            ts = tick['timestamp']
            bucket = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)

            if current_bucket != bucket:
                if current:
                    candles.append(current)
                current_bucket = bucket
                current = {
                    'timestamp': bucket.isoformat(),
                    'open': tick['price'],
                    'high': tick['price'],
                    'low': tick['price'],
                    'close': tick['price'],
                    'volume': tick['volume'],
                }
                continue

            current['high'] = max(float(current['high']), tick['price'])
            current['low'] = min(float(current['low']), tick['price'])
            current['close'] = tick['price']
            current['volume'] = float(current['volume']) + tick['volume']

        if current:
            candles.append(current)

        return candles

    def prune_ticks(self, keep_minutes: int = 120) -> None:
        cutoff = datetime.now() - timedelta(minutes=keep_minutes)
        self._ticks = [tick for tick in self._ticks if tick['timestamp'] >= cutoff]

    def opening_range(self, candles: list[dict[str, Any]]) -> dict[str, float]:
        if not candles:
            return {'high': 0.0, 'low': 0.0}
        parsed = []
        for c in candles:
            ts = datetime.fromisoformat(str(c['timestamp']))
            parsed.append((ts, c))
        session_date = max(ts.date() for ts, _ in parsed)

        start = time(9, 15)
        end = time(9, 45)
        window = [
            c for ts, c in parsed
            if ts.date() == session_date and start <= ts.time() < end
        ]
        if len(window) < 6:
            return {'high': 0.0, 'low': 0.0}
        window = sorted(window, key=lambda c: c['timestamp'])[:6]
        return {
            'high': max(float(c['high']) for c in window),
            'low': min(float(c['low']) for c in window),
        }
