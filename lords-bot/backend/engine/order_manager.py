from __future__ import annotations

import uuid
from typing import Any

from brokers.samco_client import samco_client

SUCCESS_STATUSES = {'SUCCESS', 'COMPLETE', 'FILLED'}
REQUIRED_FIELDS = {
    'exchange',
    'symbolName',
    'expiryDate',
    'strikePrice',
    'optionType',
    'transactionType',
    'orderType',
    'productType',
    'quantity',
}


class OrderManager:
    def __init__(self) -> None:
        self.paper_orders: dict[str, dict[str, Any]] = {}

    def validate_payload(self, payload: dict[str, Any]) -> tuple[bool, str]:
        missing = [key for key in REQUIRED_FIELDS if key not in payload or payload.get(key) in (None, '', 0)]
        if missing:
            return False, f'missing_fields:{",".join(sorted(missing))}'
        if str(payload.get('transactionType')).upper() not in {'BUY', 'SELL'}:
            return False, 'invalid_transaction_type'
        return True, 'ok'

    async def place_market_order(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        valid, reason = self.validate_payload(payload)
        if not valid:
            return {'status': 'Error', 'statusMessage': reason}

        if mode == 'PAPER':
            order_id = f'paper-{uuid.uuid4().hex[:8]}'
            order = {
                'status': 'Success',
                'order_id': order_id,
                'fill_price': payload.get('price', 0.0),
                'payload': payload,
                'order_status': 'COMPLETE',
            }
            self.paper_orders[order_id] = order
            return order

        response = await samco_client.place_order(payload)
        if response.get('status') == 'Success' and 'order_id' not in response:
            response['order_id'] = response.get('nOrdNo') or response.get('orderNumber') or ''
        return response

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

    def is_verified_success(self, verification: dict[str, Any]) -> bool:
        status = str(verification.get('order_status') or verification.get('status') or '').upper()
        return status in SUCCESS_STATUSES

    def handle_rejections(self, response: dict[str, Any]) -> dict[str, Any]:
        if str(response.get('status', '')).upper() == 'SUCCESS':
            return {'ok': True, 'reason': ''}
        return {'ok': False, 'reason': response.get('statusMessage', 'order_rejected')}
