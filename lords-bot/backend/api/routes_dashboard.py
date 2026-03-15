from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import performance_service, scheduler, trade_logger

router = APIRouter(tags=['dashboard'])


@router.get('/dashboard')
async def dashboard() -> dict:
    trades = trade_logger.load_trades()
    perf = performance_service.summarize(trades)
    return {
        'bot_status': scheduler.state.system_status,
        'trading_mode': scheduler.state.trading_mode,
        'market_status': {'symbol': 'NIFTY', 'spot': scheduler.state.strategy_state.get('spot', 0.0)},
        'orb_range': scheduler.state.orb_range,
        'signal_panel': scheduler.state.latest_signal,
        'trade_execution_panel': scheduler.state.active_trade,
        'trade_history': trades[-20:],
        'performance': {**perf, 'realized_pnl': scheduler.state.realized_pnl},
        'bot_controls': {'running': scheduler.running},
    }
