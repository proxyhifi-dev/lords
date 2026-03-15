from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from main_dependencies import scheduler

router = APIRouter(tags=['mode'])


class ModePayload(BaseModel):
    mode: str
    confirm_real: bool = False


@router.get('/trading-mode')
async def get_mode() -> dict:
    return {'mode': scheduler.state.trading_mode, 'enable_real_trading': settings.enable_real_trading}


@router.post('/trading-mode')
async def set_mode(payload: ModePayload) -> dict:
    mode = payload.mode.upper()
    if mode not in {'PAPER', 'REAL'}:
        raise HTTPException(status_code=400, detail='mode must be PAPER or REAL')
    if mode == 'REAL':
        if not settings.enable_real_trading:
            raise HTTPException(status_code=403, detail='real mode disabled')
        if not payload.confirm_real:
            raise HTTPException(status_code=400, detail='confirm_real=true required')
    scheduler.state.trading_mode = mode
    scheduler.state_manager.save(scheduler.state)
    return {'mode': mode}
