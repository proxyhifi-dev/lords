from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import json
import logging
from typing import Any

from brokers.samco_client import samco_client
from core.cache import TTLCache
from config import settings

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, cache: TTLCache) -> None:
        self._cache = cache
        self._last_price: float | None = None
        self._ticks: deque[dict[str, Any]] = deque(maxlen=5000)

    def _as_dict(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    def add_tick(self, price: float, timestamp: datetime | None = None) -> None:
        ts = (timestamp or datetime.utcnow()).isoformat()
        self._ticks.append({'timestamp': ts, 'price': float(price)})

    def get_recent_ticks(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(self._ticks)[-max(1, limit) :]

    async def get_nifty_spot(self) -> float:
        payload = self._as_dict(await samco_client.index_quote(settings.index_symbol))
        details = payload.get('indexDetails') or payload.get('data') or []
        if isinstance(details, list) and details:
            row = details[0]
            for key in ('spotPrice', 'ltp', 'indexValue', 'lastTradedPrice'):
                if row.get(key) not in (None, ''):
                    price = float(row[key])
                    if price > 0:
                        self._last_price = price
                        self.add_tick(price)
                        self._cache.set('nifty_spot', price, ttl_seconds=2)
                        return price

        cached = self._cache.get('nifty_spot')
        if cached:
            return float(cached)
        if self._last_price:
            logger.warning('market_data_fallback_last_live_price')
            return self._last_price
        raise RuntimeError('live_nifty_spot_unavailable')

    async def get_spot_price(self) -> float:
        return await self.get_nifty_spot()

    async def get_option_ltp(self, symbol_name: str) -> float:
        payload = self._as_dict(await samco_client.get_quote(symbol_name=symbol_name, exchange=settings.exchange))
        for key in ('lastTradedPrice', 'ltp', 'lastTradePrice'):
            value = payload.get(key)
            if value not in (None, ''):
                return float(value)
        details = payload.get('data') or payload.get('quote') or []
        if isinstance(details, list) and details:
            for key in ('lastTradedPrice', 'ltp'):
                if details[0].get(key) not in (None, ''):
                    return float(details[0][key])
        return 0.0

    async def get_historical_candles(self, symbol: str, limit: int = 120) -> list[dict[str, Any]]:
        ticks = self.get_recent_ticks(limit=max(limit * 2, 30))
        if not ticks:
            now = datetime.utcnow()
            return [
                {
                    'timestamp': (now - timedelta(minutes=i)).isoformat(),
                    'open': self._last_price or 0.0,
                    'high': self._last_price or 0.0,
                    'low': self._last_price or 0.0,
                    'close': self._last_price or 0.0,
                    'volume': 0.0,
                }
                for i in range(limit, 0, -1)
            ]
        # Approximate 1-minute bars from recent ticks
        candles: list[dict[str, Any]] = []
        bucket: dict[str, Any] | None = None
        for t in ticks:
            dt = datetime.fromisoformat(str(t['timestamp']))
            minute = dt.replace(second=0, microsecond=0)
            price = float(t['price'])
            if not bucket or bucket['timestamp'] != minute.isoformat():
                if bucket:
                    candles.append(bucket)
                bucket = {
                    'timestamp': minute.isoformat(),
                    'open': price,
                    'high': price,
                    'low': price,
                    'close': price,
                    'volume': 0.0,
                }
            else:
                bucket['high'] = max(float(bucket['high']), price)
                bucket['low'] = min(float(bucket['low']), price)
                bucket['close'] = price
        if bucket:
            candles.append(bucket)
        return candles[-limit:]
