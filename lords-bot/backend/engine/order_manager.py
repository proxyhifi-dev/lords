from __future__ import annotations

import random
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
        self.open_positions: dict[str, dict[str, Any]] = {}

    def validate_payload(self, payload: dict[str, Any]) -> tuple[bool, str]:
        missing = [key for key in REQUIRED_FIELDS if key not in payload or payload.get(key) in (None, '', 0)]
        if missing:
            return False, f'missing_fields:{",".join(sorted(missing))}'
        if str(payload.get('transactionType')).upper() not in {'BUY', 'SELL'}:
            return False, 'invalid_transaction_type'
        if str(payload.get('exchange')).upper() != 'NFO':
            return False, 'invalid_exchange_only_nfo_supported'
        return True, 'ok'

    async def _paper_fill_price(self, payload: dict[str, Any]) -> float:
        explicit_price = float(payload.get('price') or 0.0)
        if explicit_price > 0:
            return explicit_price
        quote = await samco_client.get_quote(str(payload.get('symbolName') or ''), exchange='NFO')
        ltp = float(quote.get('lastTradedPrice') or quote.get('lastTradePrice') or 0.0)
        if ltp <= 0:
            ltp = 0.01
        side = str(payload.get('transactionType') or 'BUY').upper()
        slippage = max(0.01, ltp * 0.001)
        noise = random.uniform(0.0, slippage)
        return ltp + noise if side == 'BUY' else max(0.01, ltp - noise)

    async def place_market_order(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        payload = {**payload, 'orderType': 'MARKET'}
        return await self.place_order(payload, mode)

    async def place_limit_order(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        payload = {**payload, 'orderType': 'LIMIT'}
        return await self.place_order(payload, mode)

    async def place_order(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        valid, reason = self.validate_payload(payload)
        if not valid:
            return {'status': 'Error', 'statusMessage': reason}

        mode = mode.upper()
        order_id = ''
        if mode == 'PAPER':
            order_id = f'paper-{uuid.uuid4().hex[:10]}'
            fill_price = await self._paper_fill_price(payload)
            response = {
                'status': 'Success',
                'order_id': order_id,
                'fill_price': fill_price,
                'order_status': 'COMPLETE',
                'payload': payload,
            }
            self.paper_orders[order_id] = response
        else:
            response = await samco_client.place_order(payload)
            order_id = str(response.get('order_id') or response.get('nOrdNo') or response.get('orderNumber') or '')
            if order_id:
                response['order_id'] = order_id

        if str(response.get('status', '')).lower() == 'success' and order_id:
            fill = float(response.get('fill_price') or payload.get('price') or 0.0)
            self.track_position(order_id, payload, fill_price=fill)
        return response

    async def verify_order_status(self, order_id: str, mode: str) -> dict[str, Any]:
        if mode.upper() == 'PAPER':
            return self.paper_orders.get(order_id, {'status': 'Error', 'order_status': 'NOT_FOUND'})
        return await samco_client.get_order_status(order_id)

    async def verify_order(self, order_id: str, mode: str) -> dict[str, Any]:
        return await self.verify_order_status(order_id, mode)

    async def cancel_order(self, order_id: str, mode: str) -> dict[str, Any]:
        if mode.upper() == 'PAPER':
            order = self.paper_orders.get(order_id)
            if not order:
                return {'status': 'Error', 'message': 'NOT_FOUND'}
            order['order_status'] = 'CANCELLED'
            self.open_positions.pop(order_id, None)
            return {'status': 'Success', 'order_id': order_id}
        if hasattr(samco_client.samco, 'cancel_order'):
            return await samco_client._run_io(samco_client.samco.cancel_order, order_id=order_id)
        return {'status': 'Error', 'message': 'cancel_not_supported_by_current_sdk_bridge'}

    async def flatten_positions(self, mode: str, exit_price: float) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        for order_id in list(self.open_positions.keys()):
            if mode.upper() == 'PAPER':
                closed.append(self.close_position(order_id, exit_price))
                continue
            position = self.open_positions.get(order_id) or {}
            side = str(position.get('side') or 'BUY').upper()
            close_side = 'SELL' if side == 'BUY' else 'BUY'
            payload = {
                'exchange': 'NFO',
                'symbolName': position.get('symbol'),
                'expiryDate': position.get('expiryDate'),
                'strikePrice': position.get('strikePrice'),
                'optionType': position.get('optionType'),
                'transactionType': close_side,
                'orderType': 'MARKET',
                'productType': 'MIS',
                'quantity': position.get('quantity'),
                'price': exit_price,
            }
            response = await self.place_order(payload, mode='REAL')
            if str(response.get('status', '')).lower() == 'success':
                closed.append(self.close_position(order_id, exit_price))
        return closed

    def track_position(self, order_id: str, payload: dict[str, Any], fill_price: float) -> None:
        self.open_positions[order_id] = {
            'symbol': payload.get('symbolName', ''),
            'expiryDate': payload.get('expiryDate', ''),
            'strikePrice': payload.get('strikePrice', ''),
            'optionType': payload.get('optionType', ''),
            'quantity': int(payload.get('quantity') or 0),
            'side': payload.get('transactionType', ''),
            'entry_price': float(fill_price),
            'last_price': float(fill_price),
            'pnl': 0.0,
        }

    def update_pnl(self, order_id: str, latest_price: float) -> float:
        pos = self.open_positions.get(order_id)
        if not pos:
            return 0.0
        entry = float(pos.get('entry_price') or 0.0)
        qty = int(pos.get('quantity') or 0)
        side = str(pos.get('side') or 'BUY').upper()
        pnl = (latest_price - entry) * qty
        if side == 'SELL':
            pnl *= -1
        pos['last_price'] = float(latest_price)
        pos['pnl'] = pnl
        return pnl

    def is_verified_success(self, verification: dict[str, Any]) -> bool:
        status = str(verification.get('order_status') or verification.get('status') or '').upper()
        return status in SUCCESS_STATUSES

    def close_position(self, order_id: str, exit_price: float) -> dict[str, Any]:
        pos = self.open_positions.pop(order_id, None)
        if not pos:
            return {'order_id': order_id, 'status': 'Error', 'message': 'POSITION_NOT_FOUND', 'pnl': 0.0}
        entry = float(pos.get('entry_price') or 0.0)
        qty = int(pos.get('quantity') or 0)
        side = str(pos.get('side') or 'BUY').upper()
        pnl = (float(exit_price) - entry) * qty
        if side == 'SELL':
            pnl *= -1
        return {
            'order_id': order_id,
            'symbol': pos.get('symbol', ''),
            'entry_price': entry,
            'exit_price': float(exit_price),
            'quantity': qty,
            'side': side,
            'pnl': float(pnl),
            'status': 'Success',
        }
