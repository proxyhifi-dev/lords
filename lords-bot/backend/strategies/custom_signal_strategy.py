from __future__ import annotations

from typing import Any


class CustomSignalStrategy:
    name = 'CUSTOM'

    def evaluate_signal(self, market_data: dict[str, Any]) -> dict[str, Any]:
        custom = market_data.get('custom_signal')
        if not isinstance(custom, dict):
            return {
                'signal': 'HOLD',
                'reason': 'NO_CUSTOM_SIGNAL',
                'entry_price': float(market_data.get('spot_price') or 0.0),
                'stop_loss': 0.0,
                'target_price': 0.0,
                'option_side': '',
            }
        return {
            'signal': str(custom.get('signal') or 'HOLD'),
            'reason': str(custom.get('reason') or 'USER_DEFINED'),
            'entry_price': float(custom.get('entry_price') or market_data.get('spot_price') or 0.0),
            'stop_loss': float(custom.get('stop_loss') or 0.0),
            'target_price': float(custom.get('target_price') or 0.0),
            'option_side': str(custom.get('option_side') or ''),
        }
