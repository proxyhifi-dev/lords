from __future__ import annotations

from core.cache import TTLCache


class SignalService:

    def __init__(self, cache: TTLCache) -> None:

        self.cache = cache

    def generate_signal(self, analysis: dict, option_chain: list[dict]) -> dict:

        symbol = analysis.get("symbol", "NIFTY")

        expiry = analysis.get("expiry", "")

        key = f"signal:{symbol}:{expiry}"

        cached = self.cache.get(key)

        if cached is not None:
            return cached

        pcr = float(analysis.get("pcr", 0.0) or 0.0)

        if not option_chain or pcr == 0:

            signal = {
                "signal": "NO TRADE",
                "reason": "Missing option chain or invalid PCR",
            }

        elif pcr > 1.2:

            signal = {"signal": "BUY CALL", "reason": "PCR bullish"}

        elif pcr < 0.8:

            signal = {"signal": "BUY PUT", "reason": "PCR bearish"}

        else:

            signal = {"signal": "NO TRADE", "reason": "PCR neutral"}

        self.cache.set(key, signal, 5)

        return signal