from __future__ import annotations

from typing import Any

from config import settings
from core.cache import TTLCache


class AnalysisService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    def analyze(self, option_chain: list[dict[str, Any]], symbol: str, expiry: str, underlying_price: float) -> dict[str, Any]:
        key = f'analysis:{symbol}:{expiry}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        if not option_chain:
            result = {
                'symbol': symbol,
                'expiry': expiry,
                'underlying_price': underlying_price,
                'pcr': 0.0,
                'trend': 'UNKNOWN',
                'support_level': 0.0,
                'resistance_level': 0.0,
                'max_call_oi': 0.0,
                'max_put_oi': 0.0,
                'atm_strike': 0.0,
            }
            self.cache.set(key, result, settings.analysis_ttl)
            return result

        total_put_oi = sum(float(row.get('put_oi', 0) or 0) for row in option_chain)
        total_call_oi = sum(float(row.get('call_oi', 0) or 0) for row in option_chain)
        pcr = total_put_oi / total_call_oi if total_call_oi else 0.0
        trend = 'BULLISH' if pcr > 1.2 else 'BEARISH' if pcr < 0.8 and pcr != 0 else 'SIDEWAYS'

        max_put_row = max(option_chain, key=lambda r: float(r.get('put_oi', 0) or 0))
        max_call_row = max(option_chain, key=lambda r: float(r.get('call_oi', 0) or 0))
        atm_row = min(option_chain, key=lambda r: abs(float(r.get('strike_price', 0) or 0) - underlying_price))

        result = {
            'symbol': symbol,
            'expiry': expiry,
            'underlying_price': underlying_price,
            'total_put_oi': total_put_oi,
            'total_call_oi': total_call_oi,
            'pcr': round(pcr, 4),
            'trend': trend,
            'support_level': float(max_put_row.get('strike_price', 0) or 0),
            'resistance_level': float(max_call_row.get('strike_price', 0) or 0),
            'max_call_oi': float(max_call_row.get('call_oi', 0) or 0),
            'max_put_oi': float(max_put_row.get('put_oi', 0) or 0),
            'atm_strike': float(atm_row.get('strike_price', 0) or 0),
        }
        self.cache.set(key, result, settings.analysis_ttl)
        return result
