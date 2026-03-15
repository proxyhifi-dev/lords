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
    def _to_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _normalize_split_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        strike = cls._to_float(row.get('strikePrice', row.get('strike_price')))
        ce = row.get('callOptionData', row.get('ce', {})) or {}
        pe = row.get('putOptionData', row.get('pe', {})) or {}

        return {
            'strike_price': strike,
            'call_oi': cls._to_float(ce.get('openInterest', ce.get('oi'))),
            'put_oi': cls._to_float(pe.get('openInterest', pe.get('oi'))),
            'call_change_oi': cls._to_float(ce.get('changeInOI', ce.get('change_oi'))),
            'put_change_oi': cls._to_float(pe.get('changeInOI', pe.get('change_oi'))),
            'call_ltp': cls._to_float(ce.get('lastTradedPrice', ce.get('ltp'))),
            'put_ltp': cls._to_float(pe.get('lastTradedPrice', pe.get('ltp'))),
            'call_volume': cls._to_float(ce.get('volume')),
            'put_volume': cls._to_float(pe.get('volume')),
            'volume': cls._to_float(ce.get('volume')) + cls._to_float(pe.get('volume')),
        }

    @classmethod
    def _normalize_contract_rows(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_strike: dict[float, dict[str, Any]] = {}
        for row in rows:
            strike = cls._to_float(row.get('strikePrice', row.get('strike_price')))
            if strike <= 0:
                continue

            option_type = str(row.get('optionType', row.get('option_type', ''))).upper()
            item = by_strike.setdefault(
                strike,
                {
                    'strike_price': strike,
                    'call_oi': 0.0,
                    'put_oi': 0.0,
                    'call_change_oi': 0.0,
                    'put_change_oi': 0.0,
                    'call_ltp': 0.0,
                    'put_ltp': 0.0,
                    'call_volume': 0.0,
                    'put_volume': 0.0,
                    'volume': 0.0,
                },
            )

            oi = cls._to_float(row.get('openInterest', row.get('oi')))
            change_oi = cls._to_float(row.get('changeInOI', row.get('change_oi')))
            ltp = cls._to_float(row.get('lastTradedPrice', row.get('ltp')))
            volume = cls._to_float(row.get('volume'))

            if option_type == 'CE':
                item['call_oi'] = oi
                item['call_change_oi'] = change_oi
                item['call_ltp'] = ltp
                item['call_volume'] = volume
            elif option_type == 'PE':
                item['put_oi'] = oi
                item['put_change_oi'] = change_oi
                item['put_ltp'] = ltp
                item['put_volume'] = volume
            item['volume'] = item['call_volume'] + item['put_volume']
        return [by_strike[strike] for strike in sorted(by_strike.keys())]

    @classmethod
    def normalize_chain(cls, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not raw:
            return []
        first = raw[0]
        if isinstance(first, dict) and ('callOptionData' in first or 'putOptionData' in first):
            rows = [cls._normalize_split_row(row) for row in raw if isinstance(row, dict)]
            return sorted(rows, key=lambda row: row.get('strike_price', 0.0))
        return cls._normalize_contract_rows([row for row in raw if isinstance(row, dict)])

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        key = f'market_data_cache:option_chain:{symbol}:{expiry}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        try:
            raw = await samco_client.get_option_chain(symbol, expiry)
            chain = self.normalize_chain(raw)
            self.cache.set(key, chain, settings.option_chain_ttl)
            return chain
        except Exception as exc:  # noqa: BLE001
            logger.error('option chain fetch failed symbol=%s expiry=%s err=%s', symbol, expiry, exc)
            return cached or []

    async def get_underlying_price(self, symbol: str) -> float:
        key = f'market_data_cache:underlying:{symbol}'
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
