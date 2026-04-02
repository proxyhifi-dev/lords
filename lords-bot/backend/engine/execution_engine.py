from __future__ import annotations

import asyncio
import logging
from typing import Any

from broker.samco_broker import SamcoBroker
from config import settings
from risk.risk_manager import RiskManager
from services.option_chain_service import OptionChainService
from services.state_manager import RuntimeState, StateManager
from strategies.orb_strategy import TradeSignal

logger = logging.getLogger(__name__)


class ExecutionEngine:
    def __init__(
        self,
        broker: SamcoBroker,
        option_chain_service: OptionChainService,
        risk_manager: RiskManager,
        state_manager: StateManager,
        signal_queue: asyncio.Queue[TradeSignal],
        state: RuntimeState,
    ) -> None:
        self._broker = broker
        self._option_chain = option_chain_service
        self._risk = risk_manager
        self._state_manager = state_manager
        self._signal_queue = signal_queue
        self._state = state
        self._running = False
        self._inflight_orders: set[str] = set()
        self._recent_orders: set[str] = set()

    async def _confirm_order(self, response_payload: dict[str, Any]) -> str:
        status = str(response_payload.get('orderStatus') or response_payload.get('status') or 'UNKNOWN').upper()
        if status in {'PARTIAL', 'OPEN'}:
            await asyncio.sleep(2)
        return status

    async def run(self) -> None:
        self._running = True
        while self._running:
            signal = await self._signal_queue.get()
            key = f'{signal.option_type}:{signal.strike}:{signal.action}'
            if key in self._inflight_orders or key in self._recent_orders:
                continue

            decision = self._risk.validate_trade(self._state, signal.qty)
            if not decision.allowed:
                logger.warning('trade_rejected', extra={'service': 'ExecutionEngine', 'event': 'trade_rejected', 'reason': decision.reason})
                continue

            symbol = self._option_chain.build_symbol(signal.strike, signal.option_type)
            payload = {
                'exchange': settings.exchange,
                'symbolName': symbol,
                'transactionType': signal.action,
                'orderType': 'MKT',
                'productType': 'MIS',
                'quantity': signal.qty,
            }
            self._inflight_orders.add(key)
            response = await self._broker.place_order(body=payload)
            if response.ok:
                status = await self._confirm_order(response.payload)
                self._state.open_trades[symbol] = {'qty': signal.qty, 'status': status}
                self._state.trade_count += 1
                self._state_manager.save(self._state)
                self._recent_orders.add(key)
                logger.info('order_placed', extra={'service': 'ExecutionEngine', 'event': 'order_placed', 'symbol': symbol, 'qty': signal.qty, 'status': status})
            else:
                logger.warning('order_failed', extra={'service': 'ExecutionEngine', 'event': 'order_failed', 'symbol': symbol, 'reason': response.error})
            self._inflight_orders.discard(key)

    def stop(self) -> None:
        self._running = False
