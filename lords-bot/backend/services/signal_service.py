from __future__ import annotations

from config import settings
from core.cache import TTLCache


class SignalService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    def generate_signal(self, analysis: dict) -> dict:
        symbol = analysis.get('symbol', 'NIFTY')
        expiry = analysis.get('expiry', '')
        key = f'signal:{symbol}:{expiry}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        pcr = float(analysis.get('pcr', 1.0))
        if pcr > 1.2:
            signal = {'signal': 'BUY CALL', 'reason': 'PCR > 1.2'}
        elif pcr < 0.8:
            signal = {'signal': 'BUY PUT', 'reason': 'PCR < 0.8'}
        else:
            signal = {'signal': 'NO TRADE', 'reason': 'PCR neutral zone'}

        self.cache.set(key, signal, settings.signals_ttl)
        return signal
