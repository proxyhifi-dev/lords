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

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        strike = row.get('strikePrice', row.get('strike_price', 0))
        ce = row.get('callOptionData', row.get('ce', {})) or {}
        pe = row.get('putOptionData', row.get('pe', {})) or {}

        def fv(source: dict[str, Any], *keys: str) -> float:
            for key in keys:
                if key in source:
                    try:
                        return float(source.get(key) or 0)
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0

        return {
            'strike_price': float(strike or 0),
            'call_oi': fv(ce, 'openInterest', 'oi', 'call_oi'),
            'put_oi': fv(pe, 'openInterest', 'oi', 'put_oi'),
            'call_change_oi': fv(ce, 'changeInOI', 'change_oi', 'call_change_oi'),
            'put_change_oi': fv(pe, 'changeInOI', 'change_oi', 'put_change_oi'),
            'call_ltp': fv(ce, 'lastTradedPrice', 'ltp', 'call_ltp'),
            'put_ltp': fv(pe, 'lastTradedPrice', 'ltp', 'put_ltp'),
            'volume': fv(ce, 'volume') + fv(pe, 'volume'),
        }

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        key = f'option_chain:{symbol}:{expiry}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        try:
            raw = await samco_client.get_option_chain(symbol, expiry)
            chain = [self._normalize_row(row) for row in raw if isinstance(row, dict)]
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
