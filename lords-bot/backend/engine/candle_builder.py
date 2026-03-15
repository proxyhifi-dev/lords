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
        ts_value = tick.get('timestamp')
        if not ts_value:
            return
        price = float(tick.get('price') or tick.get('ltp') or 0.0)
        volume = float(tick.get('volume') or 0.0)
        if price <= 0:
            return
        timestamp = ts_value if isinstance(ts_value, datetime) else datetime.fromisoformat(str(ts_value))
        self.reset_for_session(timestamp.date())
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
        if not self._ticks:
            return
        latest = max(t['timestamp'] for t in self._ticks)
        cutoff = latest - timedelta(minutes=keep_minutes)
        self._ticks = [tick for tick in self._ticks if tick['timestamp'] >= cutoff]

    def opening_range(self, candles: list[dict[str, Any]]) -> dict[str, float]:
        if not candles:
            return {'high': 0.0, 'low': 0.0}

        parsed = [(datetime.fromisoformat(str(c['timestamp'])), c) for c in candles]
        session_date = max(ts.date() for ts, _ in parsed)
        window = [
            c
            for ts, c in parsed
            if ts.date() == session_date and time(9, 15) <= ts.time() < time(9, 45)
        ]
        if len(window) < 6:
            return {'high': 0.0, 'low': 0.0}
        return {
            'high': max(float(c['high']) for c in window),
            'low': min(float(c['low']) for c in window),
        }
