from __future__ import annotations

from backend.config import settings
from backend.runtime_state import runtime_state
from backend.trading.paper_trading_engine import paper_trading_engine
from backend.trading.risk_manager import risk_manager


class TradeExecutor:
    def execute(self, signal: dict, symbol: str) -> dict:
        if signal.get('signal') == 'NO TRADE':
            return {'status': 'skipped', 'reason': 'no signal'}

        allowed, reason = risk_manager.can_trade()
        if not allowed:
            return {'status': 'blocked', 'reason': reason}

        if runtime_state.trading_mode == 'REAL':
            if not settings.enable_real_trading:
                return {'status': 'blocked', 'reason': 'ENABLE_REAL_TRADING=false'}
            risk_manager.register_trade()
            return {'status': 'simulated_real', 'side': signal['signal'], 'symbol': symbol}

        risk_manager.register_trade()
        return {'status': 'filled', 'trade': paper_trading_engine.place_order(signal['signal'], symbol)}


trade_executor = TradeExecutor()
