from __future__ import annotations


class SignalService:
    def generate_signal(self, analysis: dict) -> dict:
        pcr = float(analysis.get('pcr', 1.0))
        if pcr > 1.2:
            return {'signal': 'BUY CALL', 'reason': 'PCR > 1.2'}
        if pcr < 0.8:
            return {'signal': 'BUY PUT', 'reason': 'PCR < 0.8'}
        return {'signal': 'NO TRADE', 'reason': 'PCR neutral zone'}
