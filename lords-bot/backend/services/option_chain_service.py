from __future__ import annotations

from typing import Any

from brokers.samco_client import SamcoClient, samco_client
from config import settings
from core.cache import TTLCache


class OptionChainService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        key = f'option_chain:{symbol}:{expiry}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        formatted_expiry = SamcoClient.to_expiry_code(expiry)
        response = await samco_client.get_option_chain(symbol, formatted_expiry)
        details = response.get('optionChainDetails') or response.get('optionDetails') or response.get('data') or []

        chain_by_strike: dict[float, dict[str, Any]] = {}
        for row in details:
            strike = float(row.get('strikePrice') or row.get('strike_price') or 0)
            opt = str(row.get('optionType') or row.get('option_type') or '').upper()
            item = chain_by_strike.setdefault(
                strike,
                {
                    'strike_price': strike,
                    'call_oi': 0.0,
                    'put_oi': 0.0,
                    'call_ltp': 0.0,
                    'put_ltp': 0.0,
                },
            )
            if opt == 'CE':
                item['call_oi'] = float(row.get('openInterest') or row.get('open_interest') or 0)
                item['call_ltp'] = float(row.get('lastTradedPrice') or row.get('last_traded_price') or 0)
            if opt == 'PE':
                item['put_oi'] = float(row.get('openInterest') or row.get('open_interest') or 0)
                item['put_ltp'] = float(row.get('lastTradedPrice') or row.get('last_traded_price') or 0)

        normalized = sorted(chain_by_strike.values(), key=lambda x: x['strike_price'])
        self.cache.set(key, normalized, settings.option_chain_ttl)
        return normalized

    def get_option_chain_bias(self, chain: list[dict[str, Any]]) -> str:
        call_oi = sum(float(r.get('call_oi', 0)) for r in chain)
        put_oi = sum(float(r.get('put_oi', 0)) for r in chain)
        if put_oi > call_oi * 1.05:
            return 'BULLISH'
        if call_oi > put_oi * 1.05:
            return 'BEARISH'
        return 'NEUTRAL'
