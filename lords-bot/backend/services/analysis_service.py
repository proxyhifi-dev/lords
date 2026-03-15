from __future__ import annotations

from typing import Any

from config import settings
from core.cache import TTLCache


class AnalysisService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

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

        ce_rows = [row for row in option_chain if self._safe_float(row.get('call_oi')) > 0]
        pe_rows = [row for row in option_chain if self._safe_float(row.get('put_oi')) > 0]

        total_put_oi = sum(self._safe_float(row.get('put_oi')) for row in option_chain)
        total_call_oi = sum(self._safe_float(row.get('call_oi')) for row in option_chain)
        pcr = total_put_oi / total_call_oi if total_call_oi else 0.0
        trend = 'BULLISH' if pcr > 1.2 else 'BEARISH' if 0 < pcr < 0.8 else 'SIDEWAYS'

        max_put_row = max(pe_rows or option_chain, key=lambda r: self._safe_float(r.get('put_oi')))
        max_call_row = max(ce_rows or option_chain, key=lambda r: self._safe_float(r.get('call_oi')))
        atm_row = min(option_chain, key=lambda r: abs(self._safe_float(r.get('strike_price')) - underlying_price))

        result = {
            'symbol': symbol,
            'expiry': expiry,
            'underlying_price': underlying_price,
            'total_put_oi': total_put_oi,
            'total_call_oi': total_call_oi,
            'pcr': round(pcr, 4),
            'trend': trend,
            'support_level': self._safe_float(max_put_row.get('strike_price')),
            'resistance_level': self._safe_float(max_call_row.get('strike_price')),
            'max_call_oi': self._safe_float(max_call_row.get('call_oi')),
            'max_put_oi': self._safe_float(max_put_row.get('put_oi')),
            'atm_strike': self._safe_float(atm_row.get('strike_price')),
            'ce_strikes': [self._safe_float(row.get('strike_price')) for row in ce_rows],
            'pe_strikes': [self._safe_float(row.get('strike_price')) for row in pe_rows],
        }
        self.cache.set(key, result, settings.analysis_ttl)
        return result
