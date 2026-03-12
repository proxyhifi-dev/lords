from __future__ import annotations

from backend.brokers.samco_client import samco_client
from backend.config import settings
from backend.core.cache import TTLCache


class FundsService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_funds(self) -> dict:
        cached = self.cache.get('funds')
        if cached is not None:
            return cached
        payload = await samco_client.get_funds()
        self.cache.set('funds', payload, settings.funds_ttl)
        return payload
