from __future__ import annotations

import logging
from typing import Any

from brokers.samco_client import samco_client
from config import settings
from core.cache import TTLCache

logger = logging.getLogger(__name__)


class OptionChainService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        key = f'option_chain:{symbol}:{expiry}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        try:
            chain = await samco_client.get_option_chain(symbol, expiry)
            self.cache.set(key, chain, settings.option_chain_ttl)
            return chain
        except Exception as exc:  # noqa: BLE001
            logger.error('option chain fetch failed symbol=%s expiry=%s err=%s', symbol, expiry, exc)
            return cached or []

    async def get_underlying_price(self, symbol: str) -> float:
        key = f'underlying:{symbol}'
        cached = self.cache.get(key)
        if cached is not None:
            return float(cached)

        try:
            value = await samco_client.get_underlying_price(symbol)
            self.cache.set(key, value, settings.option_chain_ttl)
            return float(value)
        except Exception as exc:  # noqa: BLE001
            logger.error('underlying fetch failed symbol=%s err=%s', symbol, exc)
            return float(cached or 0.0)
