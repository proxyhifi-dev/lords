from __future__ import annotations

import logging

from brokers.samco_client import samco_client
from config import settings
from core.cache import TTLCache

logger = logging.getLogger(__name__)


class ProfileService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_profile(self) -> dict:
        cached = self.cache.get('profile')
        if cached is not None:
            return cached

        try:
            payload = await samco_client.get_profile()
            self.cache.set('profile', payload, settings.profile_ttl)
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.error('profile fetch failed err=%s', exc)
            return cached or {}
