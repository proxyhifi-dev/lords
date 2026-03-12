from __future__ import annotations

from config import runtime_state, settings
from models import SignalResult
from paper_trading_engine import paper_trading_engine


class TradeExecutor:
    def execute(self, signal: SignalResult, df, symbol: str, atm_strike: float) -> dict:
        if runtime_state.trading_mode == 'REAL':
            if not settings.enable_real_trading:
                return {'status': 'blocked', 'reason': 'REAL mode disabled by ENABLE_REAL_TRADING'}
            return {'status': 'simulated-real', 'signal': signal.signal, 'symbol': symbol, 'strike': atm_strike}

        paper_trading_engine.evaluate(signal, df, symbol, atm_strike)
        return {'status': 'paper-executed', 'signal': signal.signal}


trade_executor = TradeExecutor()
