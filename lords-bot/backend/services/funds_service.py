from __future__ import annotations

import logging

from brokers.samco_client import samco_client
from config import settings
from core.cache import TTLCache

logger = logging.getLogger(__name__)


class FundsService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_funds(self) -> dict:
        cached = self.cache.get('funds')
        if cached is not None:
            return cached

        try:
            payload = await samco_client.get_funds()
            self.cache.set('funds', payload, settings.funds_ttl)
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.error('funds fetch failed err=%s', exc)
            return cached or {}
