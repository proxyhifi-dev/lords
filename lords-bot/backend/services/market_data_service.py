from __future__ import annotations

import json
import logging
import random
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
                return {}

        return {}

    def __init__(self, cache: TTLCache) -> None:

        self.cache = cache
        self._tick_buffer: list[dict[str, Any]] = []
        self._last_price: float = 22300

    async def get_nifty_spot(self) -> float:

        cache_key = "market:nifty:spot"

        cached = self.cache.get(cache_key)

        if cached is not None:
            return float(cached)

        try:
            raw = await samco_client.index_quote("NIFTY")
            response = self._normalize_response(raw)
            details = response.get("indexDetails") or response.get("data") or []

            if not details:
                raw = await samco_client.get_quote("NIFTY")
                response = self._normalize_response(raw)
                details = response.get("indexDetails") or response.get("data") or []

            if details:

                row = details[0]

                spot = float(row.get("spotPrice") or row.get("ltp") or 0.0)

                if spot > 0:

                    self._last_price = spot

                    self.cache.set(cache_key, spot, 1)

                    return spot

        except Exception as e:

            logger.error("Samco API error: %s", e)

        # fallback when API fails
        fallback = self._last_price + random.uniform(-30, 30)

        logger.warning("Using fallback simulated price: %s", fallback)

        self._last_price = fallback

        self.cache.set(cache_key, fallback, 1)

        return fallback

    def add_tick(
        self,
        price: float,
        timestamp: datetime | None = None,
        volume: float = 0.0,
    ) -> None:

        if price <= 0:
            return

        tick_time = (timestamp or datetime.now(IST)).replace(microsecond=0)

        self._tick_buffer.append(
            {
                "timestamp": tick_time,
                "price": float(price),
                "volume": float(volume),
            }
        )

        if len(self._tick_buffer) > 7200:
            self._tick_buffer = self._tick_buffer[-7200:]

    def get_recent_ticks(self, limit: int = 2000) -> list[dict[str, Any]]:

        if limit <= 0:
            return []

        return self._tick_buffer[-limit:]
