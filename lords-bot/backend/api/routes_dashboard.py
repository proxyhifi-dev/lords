from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import performance_service, scheduler, trade_logger

router = APIRouter(tags=['dashboard'])


@router.get('/dashboard')
async def dashboard() -> dict:
    trades = trade_logger.load_trades()
    perf = performance_service.summarize(trades)
    spot = float(scheduler.state.strategy_state.get('spot') or 0.0)
    return {
        'bot_status': scheduler.state.system_status,
        'trading_mode': scheduler.state.trading_mode,
        'market_status': {'symbol': 'NIFTY 50', 'spot': spot},
        'orb_range': scheduler.state.orb_range,
        'signal_panel': scheduler.state.latest_signal,
        'active_trade': scheduler.state.active_trade,
        'trade_history': trades[-30:],
        'pnl_metrics': {
            **perf,
            'realized_pnl': scheduler.state.realized_pnl,
            'trades_today': scheduler.state.trades_today,
            'consecutive_losses': scheduler.state.consecutive_losses,
        },
        'bot_controls': {'running': scheduler.running},
        'last_error': scheduler.state.last_error,
    }
