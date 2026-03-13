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

        total_put_oi = sum(float(row.get('put_oi', 0) or 0) for row in option_chain)
        total_call_oi = sum(float(row.get('call_oi', 0) or 0) for row in option_chain)
        pcr = total_put_oi / (total_call_oi if total_call_oi else 1)
        trend = 'BULLISH' if pcr > 1.2 else 'BEARISH' if pcr < 0.8 else 'SIDEWAYS'

        result = {
            'symbol': symbol,
            'expiry': expiry,
            'underlying_price': underlying_price,
            'total_put_oi': total_put_oi,
            'total_call_oi': total_call_oi,
            'pcr': round(pcr, 4),
            'trend': trend,
        }
        self.cache.set(key, result, settings.analysis_ttl)
        return result
