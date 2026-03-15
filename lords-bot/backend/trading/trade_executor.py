from __future__ import annotations

from datetime import datetime

from config import settings
from runtime_state import runtime_state
from services.trade_log_service import trade_log_service
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

        entry = float(trade.get('entry_price', trade.get('ltp', 0)) or 0)
        stop_loss = float(trade.get('stop_loss', entry * 0.85) or 0)
        sized_qty = risk_manager.position_size(entry_price=entry, stop_loss_price=stop_loss, lot_size=int(trade.get('lot_size', 50) or 50))
        trade = {**trade, 'qty': int(trade.get('qty', sized_qty) or sized_qty), 'entry_price': entry, 'stop_loss': stop_loss}

        if mode == 'PAPER':
            position = paper_trading_engine.place_order(trade)
            order = order_manager.build_order({**trade, 'entry_price': position['entry']}, mode)
            runtime_state.orders.insert(0, order)
            trade_log_service.append({'time': datetime.utcnow().isoformat(), 'mode': mode, 'order': order, 'position': position})
            return {'status': 'filled', 'position': position, 'order': order}

        order = order_manager.build_order(trade, mode)
        runtime_state.orders.insert(0, order)
        trade_log_service.append({'time': datetime.utcnow().isoformat(), 'mode': mode, 'order': order})
        return {'status': 'sent', 'order': order}


trade_executor = TradeExecutor()
