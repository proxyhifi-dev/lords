from __future__ import annotations


class PCRStrategy:
    name = 'PCR'
    def generate(self, pcr: float) -> str:
        if pcr > 1.2:
            return 'BUY CALL'
        if 0 < pcr < 0.8:
            return 'BUY PUT'
        return 'NO TRADE'

    def evaluate_signal(self, market_data: dict) -> dict:
        chain = market_data.get('option_chain') or []
        if not chain:
            return {
                'signal': 'HOLD',
                'reason': 'PCR_UNAVAILABLE',
                'entry_price': float(market_data.get('spot_price') or 0.0),
                'stop_loss': 0.0,
                'target_price': 0.0,
                'option_side': '',
            }
        call_oi = sum(float(r.get('call_oi') or 0.0) for r in chain)
        put_oi = sum(float(r.get('put_oi') or 0.0) for r in chain)
        pcr = put_oi / max(1.0, call_oi)
        result = self.generate(pcr)
        side = 'CALL' if result == 'BUY CALL' else 'PUT' if result == 'BUY PUT' else ''
        return {
            'signal': 'BUY' if side else 'HOLD',
            'reason': f'PCR={pcr:.2f};raw={result}',
            'entry_price': float(market_data.get('spot_price') or 0.0),
            'stop_loss': 0.0,
            'target_price': 0.0,
            'option_side': side,
        }
