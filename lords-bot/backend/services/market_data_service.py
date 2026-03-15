from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from brokers.samco_client import samco_client
from config import settings
from core.cache import TTLCache

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class MarketDataService:

    @staticmethod
    def _normalize_response(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            try:
                payload = json.loads(response)
                return payload if isinstance(payload, dict) else {}
            except Exception:
                logger.warning('failed to parse market data string response')
                return {}
        return {}

    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache
        self._tick_buffer: list[dict[str, Any]] = []

    async def get_nifty_spot(self) -> float:

        cache_key = "market:nifty:spot"

        cached = self.cache.get(cache_key)

        if cached is not None:
            return float(cached)

        response = self._normalize_response(await samco_client.index_quote("NIFTY 50"))

        details = response.get("indexDetails") or response.get("data") or [{}]

        row = details[0] if details else {}

        spot = float(row.get("spotPrice") or row.get("ltp") or 0.0)

        self.cache.set(cache_key, spot, 3)

        return spot

    def add_tick(self, price: float, timestamp: datetime | None = None, volume: float = 0.0) -> None:
        if price <= 0:
            return
        tick_time = (timestamp or datetime.now(IST)).replace(microsecond=0)
        self._tick_buffer.append({'timestamp': tick_time.isoformat(), 'price': float(price), 'volume': float(volume)})
        max_ticks = 7200
        if len(self._tick_buffer) > max_ticks:
            self._tick_buffer = self._tick_buffer[-max_ticks:]

    def get_recent_ticks(self, limit: int = 2000) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return self._tick_buffer[-limit:]

    async def get_historical_candles(
        self,
        symbol: str,
        interval_minutes: int = 5,
        limit: int = 50,
    ) -> list[dict[str, Any]]:

        key = f"market:candles:{symbol}:{interval_minutes}:{limit}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        ticks = self.get_recent_ticks(limit=max(200, limit * 40))
        if not ticks:
            return []

        bucketed: dict[datetime, dict[str, Any]] = {}
        for tick in ticks:
            ts = datetime.fromisoformat(str(tick['timestamp']))
            bucket = ts.replace(minute=(ts.minute // interval_minutes) * interval_minutes, second=0, microsecond=0)
            price = float(tick.get('price') or 0.0)
            volume = float(tick.get('volume') or 0.0)
            if price <= 0:
                continue

            candle = bucketed.get(bucket)
            if candle is None:
                bucketed[bucket] = {
                    'timestamp': bucket.isoformat(),
                    'open': price,
                    'high': price,
                    'low': price,
                    'close': price,
                    'volume': volume,
                }
                continue

            candle['high'] = max(float(candle['high']), price)
            candle['low'] = min(float(candle['low']), price)
            candle['close'] = price
            candle['volume'] = float(candle['volume']) + volume

        candles = [bucketed[k] for k in sorted(bucketed.keys())]
        if limit > 0:
            candles = candles[-limit:]
        self.cache.set(key, candles, 2)
        return candles

    def compute_orb_range(self, candles: list[dict[str, Any]]) -> tuple[float, float]:

        if not candles:
            return 0.0, 0.0

        orb_candles = candles[:6]  # first 30 minutes (6 × 5min)

        high = max(float(c["high"]) for c in orb_candles)

        low = min(float(c["low"]) for c in orb_candles)

        return high, low
