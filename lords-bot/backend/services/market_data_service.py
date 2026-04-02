from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from brokers.samco_client import samco_client
from config import settings
from core.cache import TTLCache

logger = logging.getLogger(__name__)
IST = ZoneInfo('Asia/Kolkata')


class MarketDataService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache
        self._tick_buffer: deque[dict[str, Any]] = deque(maxlen=7200)
        self._last_price: float = 0.0
        self._index_candidates = [settings.index_symbol, settings.symbol, 'NIFTY 50', 'NIFTY']

    @staticmethod
    def _normalize_response(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            try:
                payload = json.loads(response)
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _extract_spot(response: dict[str, Any]) -> float:
        rows = response.get('multiQuotesDetails') or response.get('indexDetails') or response.get('data') or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ('spotPrice', 'indexLtp', 'ltp', 'lastTradedPrice'):
                value = row.get(key)
                if value is None:
                    continue
                try:
                    spot = float(value)
                except (TypeError, ValueError):
                    continue
                if spot > 0:
                    return spot
        return 0.0

    async def get_spot_price(self) -> float:
        cache_key = 'market:nifty:spot'
        cached = self.cache.get(cache_key)
        if cached is not None:
            return float(cached)

        for index_name in self._index_candidates:
            try:
                response = self._normalize_response(await samco_client.index_quote(index_name))
                spot = self._extract_spot(response)
                if spot > 0:
                    self._last_price = spot
                    self.cache.set(cache_key, spot, settings.spot_ttl)
                    return spot
                logger.warning('market_spot_invalid index=%s message=%s', index_name, response.get('message') or response.get('statusMessage'))
            except Exception:
                logger.exception('market_spot_fetch_failed index=%s', index_name)

        if self._last_price > 0:
            logger.warning('market_spot_fallback_last_price price=%s', self._last_price)
            self.cache.set(cache_key, self._last_price, 1)
            return self._last_price
        raise RuntimeError('Unable to fetch live NIFTY spot from Samco')

    async def get_nifty_spot(self) -> float:
        return await self.get_spot_price()

    def add_tick(self, price: float, timestamp: datetime | None = None, volume: float = 0.0) -> None:
        if price <= 0:
            return
        tick_time = (timestamp or datetime.now(IST)).replace(microsecond=0)
        self._tick_buffer.append({'timestamp': tick_time, 'price': float(price), 'volume': float(volume)})

    def get_recent_ticks(self, limit: int = 2000) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        data = list(self._tick_buffer)
        return data[-limit:]

    def build_candles(self, interval_minutes: int = 1, limit: int = 120) -> list[dict[str, Any]]:
        ticks = self.get_recent_ticks(limit=7200)
        if not ticks:
            return []

        candles: dict[datetime, dict[str, Any]] = {}
        bucket_minutes = max(1, interval_minutes)
        for tick in ticks:
            ts = tick.get('timestamp')
            if not ts:
                continue
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            minute_bucket = (ts.minute // bucket_minutes) * bucket_minutes
            bucket = ts.replace(minute=minute_bucket, second=0, microsecond=0)
            price = float(tick.get('price') or 0.0)
            if price <= 0:
                continue
            candle = candles.get(bucket)
            if candle is None:
                candles[bucket] = {
                    'timestamp': bucket.isoformat(),
                    'open': price,
                    'high': price,
                    'low': price,
                    'close': price,
                    'volume': float(tick.get('volume') or 0.0),
                }
                continue
            candle['high'] = max(candle['high'], price)
            candle['low'] = min(candle['low'], price)
            candle['close'] = price
            candle['volume'] += float(tick.get('volume') or 0.0)

        ordered = [candles[key] for key in sorted(candles.keys())]
        return ordered[-limit:]
