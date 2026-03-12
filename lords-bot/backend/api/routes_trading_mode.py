from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from backend.config import settings
from backend.runtime_state import runtime_state

router = APIRouter(tags=['trading-mode'])


class TradingModeRequest(BaseModel):
    mode: str


@router.get('/trading-mode')
async def get_trading_mode() -> dict:
    return {'mode': runtime_state.trading_mode, 'enable_real_trading': settings.enable_real_trading}


@router.post('/trading-mode')
async def set_trading_mode(payload: TradingModeRequest) -> dict:
    mode = payload.mode.upper()
    if mode not in {'PAPER', 'REAL'}:
        raise HTTPException(status_code=400, detail='mode must be PAPER or REAL')
    if mode == 'REAL' and not settings.enable_real_trading:
        raise HTTPException(status_code=403, detail='ENABLE_REAL_TRADING=false')
    runtime_state.trading_mode = mode
    return {'mode': runtime_state.trading_mode}
