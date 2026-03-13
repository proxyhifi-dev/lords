from __future__ import annotations

from fastapi import APIRouter, HTTPException

from runtime_state import runtime_state
from trading.paper_trading_engine import paper_trading_engine
from trading.trade_executor import trade_executor

router = APIRouter(tags=['trade'])


@router.post('/trade/approve')
async def approve_trade() -> dict:
    trade = runtime_state.pending_trade
    if not trade:
        raise HTTPException(status_code=400, detail='No pending trade to approve')
    result = trade_executor.execute({**trade, 'qty': 50})
    runtime_state.last_execution = result
    return result


@router.post('/trade/close')
async def close_positions() -> dict:
    closed = paper_trading_engine.close_all()
    return {'closed_positions': closed}


@router.post('/trade/reset')
async def reset_trading_state() -> dict:
    runtime_state.positions.clear()
    runtime_state.orders.clear()
    runtime_state.pending_trade = {}
    runtime_state.day_pnl = 0.0
    return {'status': 'reset'}
