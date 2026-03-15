from __future__ import annotations


class PCRStrategy:
    def generate(self, pcr: float) -> str:
        if pcr > 1.2:
            return 'BUY CALL'
        if 0 < pcr < 0.8:
            return 'BUY PUT'
        return 'NO TRADE'
