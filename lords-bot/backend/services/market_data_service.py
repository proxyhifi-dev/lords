from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from brokers.samco_client import samco_client
from config import settings
from core.cache import TTLCache

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_nifty_spot(self) -> float:
        cache_key = 'market:nifty:spot'
        cached = self.cache.get(cache_key)
        if cached is not None:
            return float(cached)

        response = await samco_client.index_quote('NIFTY 50')
        details = response.get('indexDetails') or response.get('data') or [{}]
        row = details[0] if details else {}
        spot = float(row.get('spotPrice') or row.get('ltp') or 0.0)
        self.cache.set(cache_key, spot, 5)
        return spot

    async def get_historical_candles(self, symbol: str, interval_minutes: int = 5, limit: int = 50) -> list[dict[str, Any]]:
        """Fallback synthetic historical candles when API endpoint is unavailable in SDK."""
        key = f'market:candles:{symbol}:{interval_minutes}:{limit}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        now = datetime.now().replace(second=0, microsecond=0)
        candles: list[dict[str, Any]] = []
        spot = await self.get_nifty_spot()
        for idx in range(limit):
            ts = now - timedelta(minutes=interval_minutes * (limit - idx))
            base = spot - (limit - idx) * 0.5
            candles.append(
                {
                    'timestamp': ts.isoformat(),
                    'open': base,
                    'high': base + 10,
                    'low': base - 10,
                    'close': base + 2,
                    'volume': 1000 + idx,
                },
            )
        self.cache.set(key, candles, settings.historical_ttl)
        return candles
