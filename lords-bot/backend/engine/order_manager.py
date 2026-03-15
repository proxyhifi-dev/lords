from __future__ import annotations

import uuid
from typing import Any

from brokers.samco_client import samco_client


class OrderManager:
    def __init__(self) -> None:
        self.paper_orders: dict[str, dict[str, Any]] = {}

    async def place_market_order(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        if mode == 'PAPER':
            order_id = f'paper-{uuid.uuid4().hex[:8]}'
            order = {'status': 'Success', 'order_id': order_id, 'fill_price': payload.get('price', 0.0), 'payload': payload}
            self.paper_orders[order_id] = {**order, 'order_status': 'COMPLETE'}
            return order
        return await samco_client.place_order(payload)

    async def place_limit_order(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        return await self.place_market_order(payload, mode)

    async def verify_order_status(self, order_id: str, mode: str) -> dict[str, Any]:
        if mode == 'PAPER':
            return self.paper_orders.get(order_id, {'status': 'Error', 'order_status': 'NOT_FOUND'})
        return await samco_client.get_order_status(order_id)

    async def cancel_order(self, order_id: str, mode: str) -> dict[str, Any]:
        if mode == 'PAPER':
            order = self.paper_orders.get(order_id)
            if not order:
                return {'status': 'Error', 'message': 'NOT_FOUND'}
            order['order_status'] = 'CANCELLED'
            return {'status': 'Success', 'order_id': order_id}
        return {'status': 'Error', 'message': 'cancel endpoint unavailable in bridge wrapper'}

    def handle_rejections(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get('status') == 'Success':
            return {'ok': True, 'reason': ''}
        return {'ok': False, 'reason': response.get('statusMessage', 'order_rejected')}
