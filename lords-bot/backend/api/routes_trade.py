from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import scheduler

router = APIRouter(tags=['trade'])


@router.post('/trade/flatten')
async def flatten() -> dict:
    scheduler.state.active_trade = {}
    scheduler.state_manager.save(scheduler.state)
    return {'status': 'flattened'}


@router.post('/trade/reset-day')
async def reset_day() -> dict:
    scheduler.state.trades_today = 0
    scheduler.state.realized_pnl = 0.0
    scheduler.state.active_trade = {}
    scheduler.state_manager.save(scheduler.state)
    return {'status': 'ok'}
