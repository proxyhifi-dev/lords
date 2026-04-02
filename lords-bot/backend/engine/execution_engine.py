from __future__ import annotations

from typing import Any

from engine.order_manager import OrderManager


class ExecutionEngine:
    def __init__(self, order_manager: OrderManager) -> None:
        self.order_manager = order_manager

    async def execute(self, order_payload: dict[str, Any], mode: str) -> dict[str, Any]:
        placed = await self.order_manager.place_market_order(order_payload, mode)
        order_id = str(placed.get('order_id') or '')
        if not order_id:
            return {'status': 'Error', 'message': 'missing_order_id', 'raw': placed}

        verification = await self.order_manager.verify_order(order_id, mode)
        if not self.order_manager.is_verified_success(verification):
            return {'status': 'Error', 'message': 'order_not_confirmed', 'raw': verification}

        fill_price = float(
            verification.get('fill_price')
            or verification.get('tradedPrice')
            or placed.get('fill_price')
            or order_payload.get('price')
            or 0.0
        )
        self.order_manager.open_positions[order_id]['entry_price'] = fill_price
        return {'status': 'Success', 'order_id': order_id, 'fill_price': fill_price, 'verification': verification}
