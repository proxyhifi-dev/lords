from __future__ import annotations

from backend.brokers.samco_client import samco_client
from backend.config import settings
from backend.core.cache import TTLCache


class ProfileService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_profile(self) -> dict:
        cached = self.cache.get('profile')
        if cached is not None:
            return cached
        payload = await samco_client.get_profile()
        self.cache.set('profile', payload, settings.profile_ttl)
        return payload
