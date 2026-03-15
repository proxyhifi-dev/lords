from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from brokers.samco_client import samco_client
from config import settings
from core.cache import TTLCache

logger = logging.getLogger(__name__)


class MarketDataService:

    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_nifty_spot(self) -> float:

        cache_key = "market:nifty:spot"

        cached = self.cache.get(cache_key)

        if cached is not None:
            return float(cached)

        response = await samco_client.index_quote("NIFTY 50")

        details = response.get("indexDetails") or response.get("data") or [{}]

        row = details[0] if details else {}

        spot = float(row.get("spotPrice") or row.get("ltp") or 0.0)

        self.cache.set(cache_key, spot, 3)

        return spot

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

        spot = await self.get_nifty_spot()

        candles: list[dict[str, Any]] = []

        now = datetime.now().replace(second=0, microsecond=0)

        for i in range(limit):

            base = spot + (i - limit / 2) * 0.5

            candles.append(
                {
                    "timestamp": now.isoformat(),
                    "open": base,
                    "high": base + 8,
                    "low": base - 8,
                    "close": base + 1,
                    "volume": 1000 + i,
                }
            )

        self.cache.set(key, candles, settings.historical_ttl)

        return candles

    def compute_orb_range(self, candles: list[dict[str, Any]]) -> tuple[float, float]:

        if not candles:
            return 0.0, 0.0

        orb_candles = candles[:6]  # first 30 minutes (6 × 5min)

        high = max(float(c["high"]) for c in orb_candles)

        low = min(float(c["low"]) for c in orb_candles)

        return high, low