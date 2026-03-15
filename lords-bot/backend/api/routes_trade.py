from __future__ import annotations

from fastapi import APIRouter, Depends

from api.auth import require_api_key
from main_dependencies import scheduler

router = APIRouter(tags=['trade'], dependencies=[Depends(require_api_key)])


@router.post('/trade/flatten')
async def flatten() -> dict:
    return await scheduler.flatten_active_trade()


@router.post('/trade/reset-day')
async def reset_day() -> dict:
    scheduler.state.trades_today = 0
    scheduler.state.realized_pnl = 0.0
    scheduler.state.active_trade = {}
    scheduler.state_manager.save(scheduler.state)
    return {'status': 'ok'}
