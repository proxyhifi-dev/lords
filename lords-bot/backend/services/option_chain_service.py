from __future__ import annotations

import json
from typing import Any

from brokers.samco_client import SamcoClient, samco_client
from core.cache import TTLCache
from config import settings


class OptionChainService:
    def __init__(self, cache: TTLCache) -> None:
        self._cache = cache
        self._live_expiry: dict[str, str] = {}

    def _as_dict(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    def _normalize(self, payload: dict[str, Any]) -> list[dict[str, float | str]]:
        details = payload.get('optionDetails') or payload.get('data') or []
        rows: dict[float, dict[str, float | str]] = {}
        for item in details if isinstance(details, list) else []:
            strike = float(item.get('strikePrice') or 0.0)
            if strike <= 0:
                continue
            row = rows.setdefault(
                strike,
                {
                    'strike_price': strike,
                    'call_oi': 0.0,
                    'put_oi': 0.0,
                    'call_ltp': 0.0,
                    'put_ltp': 0.0,
                    'expiry': SamcoClient.to_expiry_api_date(str(item.get('expiryDate') or settings.expiry or '')) if str(item.get('expiryDate') or settings.expiry or '') else '',
                },
            )
            opt_type = str(item.get('optionType') or '').upper()
            oi = float(item.get('openInterest') or 0.0)
            ltp = float(item.get('lastTradedPrice') or item.get('ltp') or 0.0)
            if opt_type == 'CE':
                row['call_oi'] = oi
                row['call_ltp'] = ltp
            elif opt_type == 'PE':
                row['put_oi'] = oi
                row['put_ltp'] = ltp
        return sorted(rows.values(), key=lambda r: float(r['strike_price']))

    async def get_option_chain(self, symbol: str, expiry: str, strike_price: str | None = None) -> list[dict[str, float | str]]:
        cache_key = f'chain:{symbol}:{expiry}:{strike_price or "all"}'
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        raw = self._as_dict(await samco_client.get_option_chain(symbol, expiry, strike_price))
        chain = self._normalize(raw)

        # Auto-roll to next live expiry when requested expiry is stale
        if not chain:
            fallback = self._as_dict(await samco_client.get_option_chain(symbol, None, strike_price))
            fallback_chain = self._normalize(fallback)
            if fallback_chain:
                live = str(fallback_chain[0].get('expiry') or '')
                if live:
                    self._live_expiry[symbol.upper()] = live
                    raw2 = self._as_dict(await samco_client.get_option_chain(symbol, live, strike_price))
                    chain = self._normalize(raw2) or fallback_chain

        if chain:
            live_expiry = str(chain[0].get('expiry') or expiry)
            self._live_expiry[symbol.upper()] = live_expiry
            self._cache.set(cache_key, chain, ttl_seconds=5)
        return chain

    def get_live_expiry(self, symbol: str, fallback: str) -> str:
        return self._live_expiry.get(symbol.upper(), fallback)

    def calculate_pcr(self, chain: list[dict[str, float | str]]) -> float:
        call_oi = sum(float(i.get('call_oi') or 0.0) for i in chain)
        put_oi = sum(float(i.get('put_oi') or 0.0) for i in chain)
        return put_oi / max(call_oi, 1.0)

    def get_option_chain_bias(self, chain: list[dict[str, float | str]]) -> str:
        pcr = self.calculate_pcr(chain)
        if pcr > 1.2:
            return 'BULLISH'
        if pcr < 0.8:
            return 'BEARISH'
        return 'NEUTRAL'

    def pick_option_contract(
        self,
        chain: list[dict[str, float | str]],
        spot_price: float,
        option_side: str,
        symbol: str,
        expiry: str,
    ) -> dict[str, Any] | None:
        if not chain:
            return None
        strike = min(chain, key=lambda r: abs(float(r.get('strike_price') or 0.0) - float(spot_price)))
        side = 'CE' if option_side.upper() in {'CALL', 'CE'} else 'PE'
        premium = float(strike.get('call_ltp') if side == 'CE' else strike.get('put_ltp') or 0.0)
        strike_price = int(float(strike.get('strike_price') or 0.0))
        expiry_code = SamcoClient.to_expiry_code(expiry)
        return {
            'option_symbol': f"{symbol.upper()}{expiry_code}{strike_price}{side}",
            'strike': str(strike_price),
            'expiry': expiry,
            'option_type': side,
            'premium': premium,
        }
