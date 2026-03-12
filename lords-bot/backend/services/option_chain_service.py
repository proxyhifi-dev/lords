from __future__ import annotations

from typing import Any

from backend.brokers.samco_client import samco_client
from backend.config import settings
from backend.core.cache import TTLCache
from backend.core.utils import samco_expiry_format


class OptionChainService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        key = f'option_chain:{symbol}:{expiry}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        chain = await samco_client.get_option_chain(symbol, samco_expiry_format(expiry))
        self.cache.set(key, chain, settings.option_chain_ttl)
        return chain

    async def get_underlying_price(self, symbol: str) -> float:
        key = f'underlying:{symbol}'
        cached = self.cache.get(key)
        if cached is not None:
            return float(cached)
        value = await samco_client.get_underlying_price(symbol)
        self.cache.set(key, value, settings.option_chain_ttl)
        return float(value)
