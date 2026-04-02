from __future__ import annotations

import asyncio
from typing import Any

from config import settings
from engine.order_manager import OrderManager


class ExecutionEngine:
    """Supports both scheduler execution and legacy signal-queue execution."""

    def __init__(
        self,
        order_manager: OrderManager | None = None,
        broker: Any | None = None,
        option_chain_service: Any | None = None,
        risk_manager: Any | None = None,
        state_manager: Any | None = None,
        signal_queue: asyncio.Queue | None = None,
        state: Any | None = None,
    ) -> None:
        self.order_manager = order_manager or OrderManager()

        # Legacy loop mode dependencies
        self._broker = broker
        self._option_chain_service = option_chain_service
        self._risk_manager = risk_manager
        self._state_manager = state_manager
        self._signal_queue = signal_queue
        self._state = state
        self._running = False
        self._inflight: set[str] = set()
        self._recent: set[str] = set()

    async def execute(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        response = await self.order_manager.place_order(payload, mode)
        if str(response.get('status', '')).lower() != 'success':
            return {'status': 'error', 'message': response.get('statusMessage') or response.get('message') or 'order_failed'}
        order_id = str(response.get('order_id') or '')
        verification = await self.order_manager.verify_order(order_id, mode)
        if self.order_manager.is_verified_success(verification):
            return {
                'status': 'success',
                'order_id': order_id,
                'fill_price': float(response.get('fill_price') or payload.get('price') or 0.0),
                'verification': verification,
            }
        return {'status': 'error', 'order_id': order_id, 'verification': verification}

    async def run(self) -> None:
        if self._signal_queue is None:
            return
        self._running = True
        while self._running:
            signal = await self._signal_queue.get()
            key = f"{signal.option_type}:{signal.strike}:{signal.action}"
            if key in self._inflight or key in self._recent:
                continue
            self._inflight.add(key)
            try:
                if self._risk_manager and self._state:
                    decision = self._risk_manager.validate_trade(self._state, signal.qty)
                    if not decision.allowed:
                        continue
                symbol = self._option_chain_service.build_symbol(signal.strike, signal.option_type)
                response = await self._broker.place_order(
                    body={
                        'exchange': settings.exchange,
                        'symbolName': symbol,
                        'transactionType': signal.action,
                        'orderType': 'MKT',
                        'productType': 'MIS',
                        'quantity': signal.qty,
                    }
                )
                if getattr(response, 'ok', False):
                    self._recent.add(key)
                if getattr(response, 'ok', False) and self._state:
                    self._state.open_trades[symbol] = {'qty': signal.qty, 'status': 'COMPLETE'}
                    self._state.trade_count += 1
                    if self._state_manager:
                        self._state_manager.save(self._state)
            finally:
                await asyncio.sleep(0.05)
                self._inflight.discard(key)

    def stop(self) -> None:
        self._running = False
