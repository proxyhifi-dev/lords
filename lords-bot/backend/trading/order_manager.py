from __future__ import annotations

from datetime import datetime
from typing import Any


class OrderManager:
    @staticmethod
    def build_order(trade: dict[str, Any], mode: str) -> dict[str, Any]:
        return {
            'time': datetime.utcnow().isoformat(),
            'symbol': trade.get('symbol'),
            'strike': trade.get('strike'),
            'type': trade.get('type'),
            'side': 'BUY',
            'qty': int(trade.get('qty', 50)),
            'price': float(trade.get('entry_price', trade.get('ltp', 0)) or 0),
            'status': 'FILLED' if mode == 'PAPER' else 'SENT',
            'mode': mode,
        }


order_manager = OrderManager()
