from __future__ import annotations

from fastapi import APIRouter

from engine.scheduler import scheduler
from runtime_state import runtime_state

router = APIRouter(tags=['option-chain'])


@router.get('/option-chain')
async def get_option_chain() -> dict:
    return {'symbol': runtime_state.symbol, 'expiry': runtime_state.expiry, 'data': runtime_state.latest_option_chain}


@router.get('/underlying-price')
async def get_underlying_price() -> dict:
    if runtime_state.latest_underlying_price == 0.0:
        runtime_state.latest_underlying_price = await scheduler.option_chain_service.get_underlying_price(runtime_state.symbol)
    return {'symbol': runtime_state.symbol, 'underlying_price': float(runtime_state.latest_underlying_price)}
