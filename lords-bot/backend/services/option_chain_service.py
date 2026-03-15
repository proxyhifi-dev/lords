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

        response = await samco_client.get_option_chain(symbol, expiry)
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
        if not chain:
            return 'NEUTRAL'

        call_oi = sum(float(r.get('call_oi', 0) or 0) for r in chain)
        put_oi = sum(float(r.get('put_oi', 0) or 0) for r in chain)
        if call_oi <= 0 and put_oi <= 0:
            return 'NEUTRAL'

        pcr = put_oi / max(1.0, call_oi)
        if pcr >= 1.1:
            return 'BULLISH'
        if pcr <= 0.9:
            return 'BEARISH'
        return 'NEUTRAL'

    def select_atm_strike(self, spot_price: float, chain: list[dict[str, Any]]) -> float:
        if not chain:
            return round(spot_price / 50.0) * 50.0

        strikes = sorted(float(row.get('strike_price') or 0.0) for row in chain if row.get('strike_price') is not None)
        if not strikes:
            return round(spot_price / 50.0) * 50.0

        return min(strikes, key=lambda strike: abs(strike - spot_price))

    def build_option_symbol(self, underlying: str, expiry: str, strike: float, option_side: str) -> str:
        expiry_code = SamcoClient.to_expiry_code(expiry)
        suffix = 'CE' if option_side.upper() == 'CALL' else 'PE'
        return f"{underlying.upper()}{expiry_code}{int(round(strike))}{suffix}"

    def pick_option_contract(
        self,
        chain: list[dict[str, Any]],
        spot_price: float,
        option_side: str,
        symbol: str,
        expiry: str,
    ) -> dict[str, Any]:
        strike = self.select_atm_strike(spot_price, chain)
        row = next((item for item in chain if float(item.get('strike_price') or 0.0) == strike), {})
        ltp_key = 'call_ltp' if option_side.upper() == 'CALL' else 'put_ltp'
        premium = float(row.get(ltp_key) or 0.0)
        return {
            'option_symbol': self.build_option_symbol(symbol, expiry, strike, option_side),
            'strike': strike,
            'premium': premium,
        }
