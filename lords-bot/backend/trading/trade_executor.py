from __future__ import annotations

from config import settings
from runtime_state import runtime_state
from trading.order_manager import order_manager
from trading.paper_trading_engine import paper_trading_engine
from trading.risk_manager import risk_manager


class TradeExecutor:
    def execute(self, trade: dict) -> dict:
        allowed, reason = risk_manager.can_trade()
        if not allowed:
            return {'status': 'blocked', 'reason': reason}

        mode = runtime_state.trading_mode
        if mode == 'REAL' and not settings.enable_real_trading:
            return {'status': 'blocked', 'reason': 'ENABLE_REAL_TRADING=false'}

        if mode == 'PAPER':
            position = paper_trading_engine.place_order(trade)
            order = order_manager.build_order({**trade, 'entry_price': position['entry']}, mode)
            runtime_state.orders.insert(0, order)
            return {'status': 'filled', 'position': position, 'order': order}

        order = order_manager.build_order(trade, mode)
        runtime_state.orders.insert(0, order)
        return {'status': 'sent', 'order': order}


trade_executor = TradeExecutor()
